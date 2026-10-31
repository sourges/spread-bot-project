import asyncio
import csv
import os
import time
import uuid
from datetime import datetime, timedelta
import ccxt
from grid import (plan_entries, exit_order, order_amount, bot_name,
                  open_positions, funding_needs, range_position, market_price)
from grid_state import save_grid_state, load_grid_state
from log_trade import log_trade
from order_cost import trades_cost


CSV_COLUMNS = ['time', 'exchange', 'pair', 'side', 'entry_price', 'exit_price',
               'amount', 'entry_cost', 'exit_cost', 'fees', 'profit']


def write_completed(filename, row):
    """Add one completed trade to a CSV file (header first if the file is new).
    One row per TRADE DONE, ready for the trade-analyzer or a spreadsheet."""
    new_file = not os.path.exists(filename)
    with open(filename, 'a', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


ERRORS_FILE = 'grid_errors.txt'     # one file for all bots, so glitches line up in time


def write_error(filename, who, step, message):
    """Add one line to the errors file:
    2026-10-29 22:25:41 | kraken CRV/USD | price check | kraken {"error":...}"""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(filename, 'a', encoding='utf-8') as f:
        f.write(f"{now} | {who} | {step} | {message}\n")


# Exchange errors that are just a temporary glitch on the exchange's side.
# ccxt already treats most of these as NetworkError, but not these, so they
# used to stop the bot. Matched against the error text.
TEMPORARY_ERRORS = (
    'EGeneral:No data',         # Kraken, e.g. Oct 29 04:01 (stopped CRV and CAKE)
)


def is_temporary(error):
    """True if this ExchangeError is a known harmless glitch: retry, don't stop."""
    return any(text in str(error) for text in TEMPORARY_ERRORS)


class OrderProblem(Exception):
    """Something happened that needs a human (e.g. an exit order was
    cancelled while we hold the coins). The bot stops and tells you."""


class GridBot:
    def __init__(self, exchange, pair, levels, usd_per_entry, amount_decimals,
                 min_amount, alert, count=2, check_interval=60,
                 state_file=None, log_file=None, csv_file=None, errors_file=None):
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
        errors_file:     None = the shared grid_errors.txt
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
        self.csv_file = csv_file or f"grid_completed_{self.name}.csv"
        self.errors_file = errors_file or ERRORS_FILE
        self.step = 'starting'      # what the bot is doing right now, for the errors file

        self.coin = pair.split('/')[0]      # 'CRV'
        self.quote = pair.split('/')[1]     # 'USD'
        self.trades = []
        self.cancelling = set()     # entry ids the bot asked to cancel (Coinbase cancels lag)
        self.total_profit = 0.0
        self.completed = 0

        # for the screen and the daily summary
        self.status = 'starting'    # 'running' or 'stopped' later
        # False until recover() has loaded the saved trades. Saving before that
        # would overwrite the state file with an empty trade list.
        self.recovered = False
        self.status_line = None     # latest "Price ... | open trades ..." text
        self.last_cycle = None      # time.monotonic() of the last cycle that finished
        self.day_done = 0           # trades done since the last daily summary
        self.day_profit = 0.0
        self.last_price = None      # price from the last cycle (for the summary)
        self.funds_alerted = set()  # sides ('long'/'short') already alerted for low funds
        self.range_state = None     # 'inside' / 'below' / 'above'; None before the first cycle

    def log(self, message):
        """Terminal line, labelled like the alerts: [kraken CRV/USD] ..."""
        print(f"[{self.label}] {message}")

    def log_error(self, message):
        """Like log(), and also written to the errors file with the time
        and the step that failed, so you can look back later."""
        self.log(message)
        try:
            write_error(self.errors_file, self.label, self.step, message)
        except OSError as e:
            print(f"[{self.label}] could not write {self.errors_file}: {e}")   # never stop trading over this

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
            # since the last 9 AM summary, saved so a restart doesn't zero them
            'day_done': self.day_done,
            'day_profit': self.day_profit,
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
            self.day_done = state.get('day_done', 0)          # older files: start at 0
            self.day_profit = state.get('day_profit', 0.0)
            await self.alert(f"🔄 GRID BOT RESTARTED - recovered {len(self.trades)} open trades.")
        self.recovered = True       # safe to save now: trades are loaded (or there were none)
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
        """USD value of what actually filled.
        Kraken: from the order's trades (exact). Otherwise from the order."""
        exact = trades_cost(self.exchange, order, self.pair)
        if exact is not None:
            return exact
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

    def tag_params(self, tag):
        """Our own name tag for an order. Each exchange calls it something different."""
        if self.exchange.id == 'coinbase':
            return {'client_order_id': tag}
        return {'clientOrderId': tag}           # Kraken: ccxt sends it as cl_ord_id

    def find_order_by_tag(self, tag):
        """The order on the exchange with this tag, or None.
        Looks at open orders first, then recently closed ones (it may have filled fast)."""
        for order in self.exchange.fetch_open_orders(self.pair):
            if order.get('clientOrderId') == tag:
                return order
        for order in self.exchange.fetch_closed_orders(self.pair):
            if order.get('clientOrderId') == tag:
                return order
        return None

    async def place_exit(self, side, amount, price, tag, max_attempts=5):
        """Exits are plain limit orders (no post-only): once we hold the
        position, getting out matters more than the fee. Retries a few
        times, then stops the bot, since we'd be holding an unprotected position.

        Before every attempt, check whether an exit with this tag already exists
        (the last attempt may have reached the exchange even though we got an error,
        or we crashed right after placing it). If so, use it instead of placing a second one."""
        for attempt in range(1, max_attempts + 1):
            try:
                self.step = f"placing exit ({side} {price})"
                existing = self.find_order_by_tag(tag)
                if existing is not None:
                    self.log(f"Exit {side} already on the exchange ({existing['id']}), not placing again.")
                    return existing['id']
                order = self.exchange.create_order(self.pair, 'limit', side, amount, price,
                                                   self.tag_params(tag))
                return order['id']
            except Exception as e:
                self.log_error(f"Exit {side} failed (attempt {attempt}/{max_attempts}): {e}")
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
        self.step = f"order check (entry {trade['entry_price']})"
        order = self.exchange.fetch_order(trade['entry_id'], self.pair)
        if order['status'] == 'closed':
            await self.entry_filled(trade, order)
        elif order['status'] in ('canceled', 'expired', 'rejected'):
            if float(order['filled']) > 0:
                await self.entry_filled(trade, order)       # partly filled before it died
            elif trade['entry_id'] in self.cancelling:
                # Our own cancel from an earlier cycle finished. Nothing unusual.
                self.cancelling.discard(trade['entry_id'])
                self.remove(trade)
                self.log(f"Cancelled {trade['side']} entry @ {trade['entry_price']}")
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
        # Make the exit's tag and SAVE it before placing, so after a crash we can find it.
        # A trade that already has a tag (crash last time) keeps it.
        if not trade.get('exit_tag'):
            trade['exit_tag'] = str(uuid.uuid4())
            self.save()
        trade['exit_id'] = await self.place_exit(exit_side['side'], exit_side['amount'],
                                                 exit_side['price'], trade['exit_tag'])
        trade['status'] = 'exit_open'
        self.save()

        log_trade(f"{trade['side']} entry", trade['entry_price'], filled,
                  trade['entry_id'], self.log_file)
        await self.alert(f"✅ {trade['side'].upper()} ENTRY FILLED\n"
                         f"{filled} {self.coin} @ {trade['entry_price']}\n"
                         f"Exit {exit_side['side']} placed @ {trade['exit_price']}")

    async def check_exit(self, trade):
        self.step = f"order check (exit {trade['exit_price']})"
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

        write_completed(self.csv_file, {
            'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'exchange': self.exchange.id, 'pair': self.pair, 'side': trade['side'],
            'entry_price': trade['entry_price'], 'exit_price': trade['exit_price'],
            'amount': trade['amount'], 'entry_cost': round(entry_cost, 4),
            'exit_cost': round(exit_cost, 4), 'fees': round(fees, 4),
            'profit': round(profit, 4),
        })
        self.total_profit += profit
        self.completed += 1
        self.day_done += 1
        self.day_profit += profit
        self.remove(trade)

        log_trade(f"{trade['side']} exit (profit {profit:.4f})", trade['exit_price'],
                  order['filled'], trade['exit_id'], self.log_file)
        await self.alert(f"💰 {trade['side'].upper()} TRADE DONE\n"
                         f"{trade['entry_price']} → {trade['exit_price']}, {trade['amount']} {self.coin}\n"
                         f"Profit: ${profit:.4f}\n"
                         f"📊 Total: {self.completed} trades | ${self.total_profit:.4f}")

    # ---------- step 2: cancel entries that are too far away ----------

    async def cancel_entry(self, trade):
        self.step = f"cancel (entry {trade['entry_price']})"
        try:
            self.exchange.cancel_order(trade['entry_id'], self.pair)
        except (ccxt.OrderNotFound, ccxt.InvalidOrder) as e:
            self.log(f"Cancel of {trade['entry_id']} failed ({e}), probably already filled.")

        # Always check what really happened. It may have filled just before
        # the cancel arrived (the "cancel race").
        order = self.exchange.fetch_order(trade['entry_id'], self.pair)
        if order['status'] == 'open':
            # Some exchanges (Coinbase) take a moment to cancel. Remember we asked,
            # so next cycle's check doesn't report it as cancelled "outside the bot".
            self.cancelling.add(trade['entry_id'])
            return
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
            self.step = f"placing entry ({side} {trade['entry_price']})"
            try:
                amount = order_amount(self.usd_per_entry, trade['entry_price'],
                                      self.amount_decimals, self.min_amount)
                trade['entry_id'] = self.place_entry(side, amount, trade['entry_price'])
            except ccxt.NetworkError:
                raise                           # let the main loop wait and retry
            except ccxt.InsufficientFunds as e:
                # Alert once per side, then stay quiet until an entry on that side works.
                if trade['side'] not in self.funds_alerted:
                    self.funds_alerted.add(trade['side'])
                    currency = self.quote if trade['side'] == 'long' else self.coin
                    await self.alert(f"⚠️ Not enough {currency} for {trade['side']} entry "
                                     f"@ {trade['entry_price']}. Will keep trying every cycle "
                                     f"(no more alerts until a {trade['side']} entry works).")
                self.log_error(f"Not enough funds for {trade['side']} entry @ {trade['entry_price']}: {e}")
                continue
            except Exception as e:
                # e.g. post-only rejected because price moved. Nothing is at risk,
                # so just try again next cycle.
                self.log_error(f"Could not place {trade['side']} entry @ {trade['entry_price']}: {e}")
                continue
            self.funds_alerted.discard(trade['side'])      # funds are fine again
            trade['amount'] = amount
            self.trades.append(trade)
            self.save()
            self.log(f"Placed {trade['side']} entry: {side} {amount} {self.coin} @ {trade['entry_price']}")

    # ---------- the loop ----------

    async def run_cycle(self):
        await self.check_fills()                                  # 1. fills first, so statuses are current
        self.step = 'price check'
        price = market_price(self.exchange.fetch_ticker(self.pair))   # bid/ask mid, else last
        await self.check_range(price)
        plan = plan_entries(self.levels, price, self.trades, self.count)
        for trade in plan['cancel']:                              # 2. cancel far entries
            await self.cancel_entry(trade)
        await self.place_entries(price)                           # 3. re-plan, then place
        # Not printed here: print_status() prints every bot's line together, once a minute.
        self.status_line = (f"Price {price} | open trades: {len(self.trades)} | "
                            f"done: {self.completed} | profit ${self.total_profit:.4f}")
        if self.range_state != 'inside':
            self.status_line += f" | OUT OF RANGE ({self.range_state})"
        self.last_cycle = time.monotonic()
        self.last_price = price

    async def check_range(self, price):
        """Alert ONCE when the price leaves the grid's range, and once when it's back.
        Trading doesn't change: past the ends there are simply no levels to place."""
        new = range_position(self.levels, price)
        if new == self.range_state:
            return
        lower, upper = self.levels[0], self.levels[-1]
        if new == 'below':
            await self.alert(f"⚠️ OUT OF RANGE: price {price} is BELOW the range ({lower}).\n"
                             f"No new buys until it's back above {lower}. Open exits still work.")
        elif new == 'above':
            await self.alert(f"⚠️ OUT OF RANGE: price {price} is ABOVE the range ({upper}).\n"
                             f"No new sells until it's back below {upper}. Open exits still work.")
        elif self.range_state is not None:
            # back inside (no alert on a normal start inside the range)
            await self.alert(f"✅ BACK IN RANGE: price {price} ({lower}–{upper}).")
        self.range_state = new

    async def run_continuous(self):
        try:
            await self.recover()        # inside try, so a recovery problem sends 🛑
            self.status = 'running'
            while True:
                try:
                    await self.run_cycle()
                except ccxt.NetworkError as e:
                    # Internet or Kraken hiccup: nothing changed, just try again.
                    self.log_error(f"Network problem, retrying next cycle: {e}")
                except ccxt.ExchangeError as e:
                    if not is_temporary(e):
                        raise                   # anything else: stop (handled below)
                    # Still visible if it keeps happening: "no cycle for 10 min"
                    # on screen and in the 9 AM summary.
                    self.log_error(f"Exchange glitch, retrying next cycle: {e}")
                await asyncio.sleep(self.check_interval)
        except OrderProblem as e:
            self.status = 'stopped'
            self.log_error(f"STOPPED - order problem: {e}")
            await self.alert(f"🛑 GRID BOT STOPPED - order problem\n{e}\n"
                             "Check the exchange before restarting.")
            print(e)
        except asyncio.CancelledError:
            self.status = 'stopped'
            await self.alert(f"⏹️ GRID BOT STOPPED\nOpen trades: {len(self.trades)}\n"
                             f"Done: {self.completed} | Profit: ${self.total_profit:.4f}")
            raise
        except Exception as e:
            # Anything unexpected: stop instead of looping blindly with real money.
            self.status = 'stopped'
            self.log_error(f"STOPPED - unexpected error: {type(e).__name__}: {e}")
            await self.alert(f"🛑 GRID BOT STOPPED - unexpected error\n{type(e).__name__}: {e}")
            raise


async def run_bots(bots, helpers=()):
    """Run several grid bots at the same time.

    Each bot stops on its own problems without stopping the others:
    return_exceptions=True means one bot's crash is collected instead of
    cancelling the rest. Ctrl+C still stops them all, and each sends its
    own STOPPED alert.

    helpers: extra loops that run alongside the bots (screen printout,
    daily summary). They stop when the bots stop."""
    helper_tasks = [asyncio.create_task(h) for h in helpers]
    try:
        await asyncio.gather(*(bot.run_continuous() for bot in bots),
                             return_exceptions=True)
    finally:
        for task in helper_tasks:
            task.cancel()


# ---------- once a minute: one block on screen ----------

def minutes_since(bot, now):
    """Whole minutes since the bot's last finished cycle, or None if it never finished one."""
    if bot.last_cycle is None:
        return None
    return int((now - bot.last_cycle) // 60)


def status_lines(bots, now, stale_minutes=10):
    """One screen line per bot."""
    lines = []
    for bot in bots:
        if bot.status == 'stopped':
            lines.append(f"[{bot.label}] STOPPED")
        elif bot.status_line is None:
            lines.append(f"[{bot.label}] starting...")
        else:
            line = f"[{bot.label}] {bot.status_line}"
            age = minutes_since(bot, now)
            if age >= stale_minutes:
                line += f"  ⚠️ no cycle for {age} min"
            lines.append(line)
    return lines


async def print_status(bots, interval):
    """Every interval seconds: a dashed line with the time, then one line per bot."""
    while True:
        await asyncio.sleep(interval)
        print(f"{'-' * 30} {datetime.now():%H:%M:%S} {'-' * 30}")
        for line in status_lines(bots, time.monotonic()):
            print(line)


# ---------- once a day: Telegram summary ----------

def seconds_until(hour, now):
    """Seconds from now until the next time the clock shows hour:00."""
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def money(x):
    """+$1.25 or -$0.44"""
    sign = '-' if x < 0 else '+'
    return f"{sign}${abs(x):.2f}"


def summary_text(bots, now, stale_minutes=10):
    lines = ["📋 DAILY GRID SUMMARY (since last summary)"]
    day_done = 0
    day_profit = 0.0
    total_profit = 0.0
    open_pnl = 0.0
    for bot in bots:
        entries = sum(1 for t in bot.trades if t['status'] == 'entry_open')
        exits = sum(1 for t in bot.trades if t['status'] == 'exit_open')
        if bot.status == 'stopped':
            head = f"🛑 {bot.label}: STOPPED"
        else:
            head = f"{bot.label}"
            if bot.range_state in ('below', 'above'):
                edge = bot.levels[0] if bot.range_state == 'below' else bot.levels[-1]
                head = f"⚠️ {bot.label}: OUT OF RANGE ({bot.range_state} {edge})"
            age = minutes_since(bot, now)
            if age is None or age >= stale_minutes:
                head = f"⚠️ {bot.label}: " + ("no cycle yet" if age is None else f"no cycle for {age} min")
        lines.append(f"\n{head}\n"
                     f"  {bot.day_done} done, ${bot.day_profit:.2f}\n"
                     f"  waiting: {entries} entries, {exits} exits\n"
                     f"  all time: {bot.completed} done, ${bot.total_profit:.2f}")
        if bot.last_price is not None:
            held = open_positions(bot.trades, bot.last_price)
            open_pnl += held['open_pnl']
            if held['longs']:
                lines.append(f"  holding: {held['longs']} longs, {held['long_amount']:g} {bot.coin}, "
                             f"cost ${held['long_cost']:.2f}, now ${held['long_value']:.2f} "
                             f"({money(held['long_value'] - held['long_cost'])})")
            if held['shorts']:
                lines.append(f"  shorts: {held['shorts']}, {held['short_amount']:g} {bot.coin} "
                             f"sold for ${held['short_proceeds']:.2f}, buy back now "
                             f"${held['short_buyback']:.2f} "
                             f"({money(held['short_proceeds'] - held['short_buyback'])})")
        day_done += bot.day_done
        day_profit += bot.day_profit
        total_profit += bot.total_profit
    lines.append(f"\n📊 ALL BOTS: {day_done} done, ${day_profit:.2f} | all time ${total_profit:.2f}"
                 f"\n   open trades if closed now: {money(open_pnl)}")
    return "\n".join(lines)


async def send_summary(bots, alert, now):
    """Send the summary, then start counting the next day from zero."""
    await alert(summary_text(bots, now))
    for bot in bots:
        bot.day_done = 0
        bot.day_profit = 0.0
        if bot.recovered:
            bot.save()              # so a restart after 9 AM doesn't bring back yesterday's counts


async def daily_summary(bots, alert, hour):
    """Sleep until hour:00 on the PC's clock, send the summary, repeat."""
    while True:
        await asyncio.sleep(seconds_until(hour, datetime.now()))
        try:
            await send_summary(bots, alert, time.monotonic())
        except Exception as e:
            print(f"[SUMMARY ERROR] {e}")     # never let the summary stop the bots


# ---------- at startup: is the whole range funded? ----------

def funding_report(rows, balances):
    """rows: one per bot, (exchange_id, label, quote, coin, needs) where needs comes
    from funding_needs(). balances: {exchange_id: {currency: total balance}}.
    Bots on the same exchange share money, so needs are added up per currency.
    Returns (lines to print, list of shortfall lines)."""
    lines = []
    totals = {}             # (exchange_id, currency) -> amount needed by all bots
    for ex, label, quote, coin, needs in rows:
        lines.append(f"  {label}: whole range needs {needs['quote']:,.2f} {quote} "
                     f"and {needs['coin']:,.2f} {coin}")
        totals[(ex, quote)] = totals.get((ex, quote), 0.0) + needs['quote']
        totals[(ex, coin)] = totals.get((ex, coin), 0.0) + needs['coin']
    short = []
    for (ex, currency), needed in totals.items():
        have = balances.get(ex, {}).get(currency) or 0.0
        if have < needed:
            short.append(f"{ex} {currency}: need {needed:,.2f}, have {have:,.2f} "
                         f"(short {needed - have:,.2f})")
    return lines, short


async def check_funding(bots, alert):
    """Print what each bot's whole range needs vs. what the exchange holds.
    Alerts once if something is short. Never stops trading: it's a warning only.
    Other things using the same money (spread bot, DCA, 3commas) aren't counted."""
    try:
        rows = []
        exchanges = {}
        for bot in bots:
            state = load_grid_state(bot.state_file)
            trades = state['trades'] if state else []
            levels = state['levels'] if state else bot.levels
            price = market_price(bot.exchange.fetch_ticker(bot.pair))
            needs = funding_needs(levels, price, trades, bot.usd_per_entry)
            rows.append((bot.exchange.id, bot.label, bot.quote, bot.coin, needs))
            exchanges[bot.exchange.id] = bot.exchange
        balances = {name: ex.fetch_balance()['total'] for name, ex in exchanges.items()}
        lines, short = funding_report(rows, balances)
        print("\n💰 Funding check (whole range, includes open exits):")
        for line in lines:
            print(line)
        if short:
            await alert("⚠️ FUNDING: not enough to cover the whole range\n" + "\n".join(short)
                        + "\nBots still run; entries will fail once the money runs out.")
        else:
            print("  ✅ every range is fully funded")
    except Exception as e:
        print(f"⚠️ Funding check failed ({e}). Bots start anyway.")
