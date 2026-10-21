import asyncio
import ccxt
from grid import plan_entries, exit_order, order_amount, bot_name
from grid_state import save_grid_state, load_grid_state
from log_trade import log_trade


class OrderProblem(Exception):
    """Something happened that needs a human (e.g. an exit order was
    cancelled while we hold the coins). The bot stops and tells you."""


class GridBot:
    def __init__(self, exchange, pair, levels, usd_per_entry, amount_decimals,
                 min_amount, alert, count=2, check_interval=60,
                 state_file=None, log_file=None):
        """
        exchange:        ccxt exchange object (Kraken now, Coinbase later)
        levels:          grid prices from geometric_levels()
        usd_per_entry:   dollars per entry order (e.g. 61)
        amount_decimals: Kraken's amount precision as decimals
        min_amount:      Kraken's minimum order size in coins
        alert:           async function that sends a Telegram message
        count:           live entries per side (2 = 2 buys + 2 sells)
        state_file, log_file: leave as None to name them after the bot,
                         e.g. grid_state_kraken_CRV_USD.json
        """
        self.exchange = exchange
        self.pair = pair
        self.levels = levels
        self.usd_per_entry = usd_per_entry
        self.amount_decimals = amount_decimals
        self.min_amount = min_amount
        self.send = alert                   # raw sender; use self.alert() instead
        self.count = count
        self.check_interval = check_interval
        self.name = bot_name(exchange.id, pair)          # 'kraken_CRV_USD'
        self.label = f"{exchange.id} {pair}"             # 'kraken CRV/USD'
        self.state_file = state_file or f"grid_state_{self.name}.json"
        self.log_file = log_file or f"grid_trades_{self.name}.txt"

        self.coin = pair.split('/')[0]      # 'CRV'
        self.quote = pair.split('/')[1]     # 'USD'
        self.trades = []
        self.total_profit = 0.0
        self.completed = 0

    def log(self, message):
        """Terminal line, labelled like the alerts: [kraken CRV/USD] ..."""
        print(f"[{self.label}] {message}")

    async def alert(self, message):
        """Every alert starts with the bot's label, so with several bots
        you know which one is talking."""
        await self.send(f"[{self.label}] {message}")

    # ---------- state ----------

    def save(self):
        """Save everything needed to restart. Called after EVERY change."""
        state = {
            'exchange': self.exchange.id,
            'pair': self.pair,
            'levels': self.levels,
            'trades': self.trades,
            'total_profit': self.total_profit,
            'completed': self.completed,
        }
        save_grid_state(state, self.state_file)

    async def recover(self):
        """On startup: reload saved trades and levels. The grid is NOT rebuilt,
        because the saved trades refer to the saved levels."""
        state = load_grid_state(self.state_file)
        if state is not None:
            # Files saved before v2 have no pair/exchange, so only check if present.
            saved = (state.get('exchange', self.exchange.id), state.get('pair', self.pair))
            if saved != (self.exchange.id, self.pair):
                raise OrderProblem(f"{self.state_file} belongs to {saved[0]} {saved[1]}, "
                                   f"not {self.label}. Nothing was placed.")
            if state['levels'] != self.levels:
                await self.alert("⚠️ Grid settings changed since last run. "
                                 "Using the SAVED levels so open trades still match.")
            self.levels = state['levels']
            self.trades = state['trades']
            self.total_profit = state['total_profit']
            self.completed = state['completed']
            await self.alert(f"🔄 GRID BOT RESTARTED - recovered {len(self.trades)} open trades.")
        await self.report_unknown_orders()

    async def report_unknown_orders(self):
        """Warn about open orders on this pair that the bot doesn't know about
        (e.g. left over from 3commas, or placed just before a crash).
        It only warns. It never cancels orders it didn't make."""
        known = set()
        for trade in self.trades:
            known.add(trade['entry_id'])
            known.add(trade['exit_id'])
        open_orders = self.exchange.fetch_open_orders(self.pair)
        unknown = [o for o in open_orders if o['id'] not in known]
        if unknown:
            lines = [f"{o['side']} {o['amount']} @ {o['price']} ({o['id']})" for o in unknown]
            await self.alert("⚠️ Open orders on " + self.pair + " the grid bot did not place:\n"
                             + "\n".join(lines) + "\nCheck the exchange. The bot will leave them alone.")

    # ---------- small helpers ----------

    def fee_usd(self, order):
        """Fee paid on an order, in USD. 0 if Kraken didn't report one."""
        fee = order.get('fee')
        if not fee or fee.get('cost') is None:
            return 0.0
        cost = float(fee['cost'])
        if fee.get('currency') == self.quote:
            return cost
        price = float(order.get('average') or order['price'])
        return cost * price                 # fee was charged in coins

    def order_cost(self, order):
        """USD value of what actually filled."""
        # float() everywhere: Kraken/ccxt sometimes hands back numbers as text
        if order.get('cost'):
            return float(order['cost'])
        price = float(order.get('average') or order['price'])
        return float(order['filled']) * price

    def remove(self, trade):
        self.trades.remove(trade)
        self.save()

    # ---------- placing orders ----------

    def place_entry(self, side, amount, price):
        """Entries are post-only: Kraken rejects them instead of filling as
        taker, so we always pay the maker fee."""
        order = self.exchange.create_order(self.pair, 'limit', side, amount, price,
                                           {'postOnly': True})
        return order['id']

    async def place_exit(self, side, amount, price, max_attempts=5):
        """Exits are plain limit orders (no post-only): once we hold the
        position, getting out matters more than the fee. Retries a few
        times, then stops the bot, since we'd be holding an unprotected position."""
        for attempt in range(1, max_attempts + 1):
            try:
                order = self.exchange.create_order(self.pair, 'limit', side, amount, price)
                return order['id']
            except Exception as e:
                self.log(f"Exit {side} failed (attempt {attempt}/{max_attempts}): {e}")
                if attempt < max_attempts:
                    await asyncio.sleep(self.check_interval)
        raise OrderProblem(f"Could not place exit {side} {amount} {self.coin} @ {price}.")

    # ---------- step 1: check fills ----------

    async def check_fills(self):
        # loop over a copy, because trades get removed inside the loop
        for trade in list(self.trades):
            if trade['status'] == 'entry_open':
                await self.check_entry(trade)
            elif trade['status'] == 'exit_open':
                await self.check_exit(trade)

    async def check_entry(self, trade):
        order = self.exchange.fetch_order(trade['entry_id'], self.pair)
        if order['status'] == 'closed':
            await self.entry_filled(trade, order)
        elif order['status'] in ('canceled', 'expired', 'rejected'):
            if float(order['filled']) > 0:
                await self.entry_filled(trade, order)       # partly filled before it died
            else:
                self.remove(trade)
                await self.alert(f"⚠️ {trade['side']} entry @ {trade['entry_price']} was "
                                 f"{order['status']} outside the bot. Removed it.")
        # 'open' (even partly filled): keep waiting

    async def entry_filled(self, trade, order):
        """Entry filled: place the exit for exactly the amount that filled."""
        filled = float(order['filled'])
        if filled < self.min_amount:
            # Too small for Kraken to accept an exit. Drop it and tell the user.
            self.remove(trade)
            log_trade(f"{trade['side']} entry (dust)", trade['entry_price'], filled,
                      trade['entry_id'], self.log_file)
            await self.alert(f"⚠️ {trade['side']} entry @ {trade['entry_price']} only filled "
                             f"{filled} {self.coin}, below the minimum to exit. Left in your balance.")
            return

        exit_side = exit_order(trade, filled)
        trade['amount'] = filled
        trade['entry_cost'] = self.order_cost(order)
        trade['entry_fee'] = self.fee_usd(order)
        trade['exit_id'] = await self.place_exit(exit_side['side'], exit_side['amount'],
                                                 exit_side['price'])
        trade['status'] = 'exit_open'
        self.save()

        log_trade(f"{trade['side']} entry", trade['entry_price'], filled,
                  trade['entry_id'], self.log_file)
        await self.alert(f"✅ {trade['side'].upper()} ENTRY FILLED\n"
                         f"{filled} {self.coin} @ {trade['entry_price']}\n"
                         f"Exit {exit_side['side']} placed @ {trade['exit_price']}")

    async def check_exit(self, trade):
        order = self.exchange.fetch_order(trade['exit_id'], self.pair)
        if order['status'] == 'closed':
            await self.exit_filled(trade, order)
        elif order['status'] in ('canceled', 'expired', 'rejected'):
            raise OrderProblem(f"Exit order {trade['exit_id']} ({trade['side']} trade, "
                               f"{trade['amount']} {self.coin} @ {trade['exit_price']}) was "
                               f"{order['status']}. You hold this position with no exit.")

    async def exit_filled(self, trade, order):
        exit_cost = self.order_cost(order)
        # float(): trades saved before the Oct 15 fix may hold these as text
        entry_cost = float(trade['entry_cost'])
        fees = float(trade['entry_fee']) + self.fee_usd(order)
        if trade['side'] == 'long':
            profit = exit_cost - entry_cost - fees     # bought low, sold high
        else:
            profit = entry_cost - exit_cost - fees     # sold high, bought back low

        self.total_profit += profit
        self.completed += 1
        self.remove(trade)

        log_trade(f"{trade['side']} exit (profit {profit:.4f})", trade['exit_price'],
                  order['filled'], trade['exit_id'], self.log_file)
        await self.alert(f"💰 {trade['side'].upper()} TRADE DONE\n"
                         f"{trade['entry_price']} → {trade['exit_price']}, {trade['amount']} {self.coin}\n"
                         f"Profit: ${profit:.4f}\n"
                         f"📊 Total: {self.completed} trades | ${self.total_profit:.4f}")

    # ---------- step 2: cancel entries that are too far away ----------

    async def cancel_entry(self, trade):
        try:
            self.exchange.cancel_order(trade['entry_id'], self.pair)
        except (ccxt.OrderNotFound, ccxt.InvalidOrder) as e:
            self.log(f"Cancel of {trade['entry_id']} failed ({e}), probably already filled.")

        # Always check what really happened. It may have filled just before
        # the cancel arrived (the "cancel race").
        order = self.exchange.fetch_order(trade['entry_id'], self.pair)
        if order['status'] == 'open':
            return                              # cancel didn't go through; try next cycle
        if float(order['filled']) > 0:
            await self.entry_filled(trade, order)
        else:
            self.remove(trade)
            self.log(f"Cancelled {trade['side']} entry @ {trade['entry_price']}")

    # ---------- step 3: place new entries ----------

    async def place_entries(self, price):
        plan = plan_entries(self.levels, price, self.trades, self.count)
        for trade in plan['place']:
            side = 'buy' if trade['side'] == 'long' else 'sell'
            try:
                amount = order_amount(self.usd_per_entry, trade['entry_price'],
                                      self.amount_decimals, self.min_amount)
                trade['entry_id'] = self.place_entry(side, amount, trade['entry_price'])
            except ccxt.NetworkError:
                raise                           # let the main loop wait and retry
            except Exception as e:
                # e.g. post-only rejected because price moved. Nothing is at risk,
                # so just try again next cycle.
                self.log(f"Could not place {trade['side']} entry @ {trade['entry_price']}: {e}")
                continue
            trade['amount'] = amount
            self.trades.append(trade)
            self.save()
            self.log(f"Placed {trade['side']} entry: {side} {amount} {self.coin} @ {trade['entry_price']}")

    # ---------- the loop ----------

    async def run_cycle(self):
        await self.check_fills()                                  # 1. fills first, so statuses are current
        price = self.exchange.fetch_ticker(self.pair)['last']
        plan = plan_entries(self.levels, price, self.trades, self.count)
        for trade in plan['cancel']:                              # 2. cancel far entries
            await self.cancel_entry(trade)
        await self.place_entries(price)                           # 3. re-plan, then place
        self.log(f"Price {price} | open trades: {len(self.trades)} | "
              f"done: {self.completed} | profit ${self.total_profit:.4f}")

    async def run_continuous(self):
        try:
            await self.recover()        # inside try, so a recovery problem sends 🛑
            while True:
                try:
                    await self.run_cycle()
                except ccxt.NetworkError as e:
                    # Internet or Kraken hiccup: nothing changed, just try again.
                    self.log(f"Network problem, retrying next cycle: {e}")
                await asyncio.sleep(self.check_interval)
        except OrderProblem as e:
            await self.alert(f"🛑 GRID BOT STOPPED - order problem\n{e}\n"
                             "Check the exchange before restarting.")
            print(e)
        except asyncio.CancelledError:
            await self.alert(f"⏹️ GRID BOT STOPPED\nOpen trades: {len(self.trades)}\n"
                             f"Done: {self.completed} | Profit: ${self.total_profit:.4f}")
            raise
        except Exception as e:
            # Anything unexpected: stop instead of looping blindly with real money.
            await self.alert(f"🛑 GRID BOT STOPPED - unexpected error\n{type(e).__name__}: {e}")
            raise


async def run_bots(bots):
    """Run several grid bots at the same time.

    Each bot stops on its own problems without stopping the others:
    return_exceptions=True means one bot's crash is collected instead of
    cancelling the rest. Ctrl+C still stops them all, and each sends its
    own STOPPED alert."""
    await asyncio.gather(*(bot.run_continuous() for bot in bots),
                         return_exceptions=True)
