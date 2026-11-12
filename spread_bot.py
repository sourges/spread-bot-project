import asyncio
import time
from datetime import datetime
import ccxt
from telegram import Bot
from log_trade import log_trade
from state import save_state, load_state, save_totals, load_totals
from telegram_send import send_with_retries
from order_cost import trades_cost


class OrderProblem(Exception):
    """Raised when an order is cancelled/expired/rejected, or can't be placed.
    The bot stops and tells you, instead of waiting forever."""


def is_post_only_error(error):
    """True if placing failed because the post-only order would have taken
    liquidity (filled as a taker). Kraken may say so when the order is placed."""
    text = str(error).lower()
    return 'post only' in text or 'post-only' in text or 'post_only' in text


def step_price(normal, side, steps, step):
    """Price after `steps` steps away from normal: a sell steps UP, a buy steps DOWN.
    e.g. sell 1.0, 1 step of 0.0001 -> 1.0001; buy 0.9999, 1 step -> 0.9998"""
    direction = 1 if side == 'sell' else -1
    return round(normal + direction * steps * step, 8)


class SpreadBot:
    def __init__(self, exchange, pair, buy_price, sell_price, amount, 
                 telegram_token, chat_id, check_interval=60, totals_file='spread_totals.json',
                 state_file='state.json', compound=False, step=0.0001,
                 tries_per_price=5, move_back_every=3600, confirm_delay=2, max_failures=5):
        """
        Initialize the spread bot
        """
        self.exchange = exchange
        self.pair = pair
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.amount = amount
        # compound=True: profit is added to each new buy (see buy_amount)
        self.compound = compound
        self.telegram_bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.check_interval = check_interval
        
        self.buy_order_id = None
        self.sell_order_id = None
        self.totals_file = totals_file
        self.state_file = state_file
        # ----- post-only + stepping (see place_order and maybe_move_back) -----
        self.step = step                        # 0.0001: one price step
        self.tries_per_price = tries_per_price  # rejected this many times -> step one further
        self.move_back_every = move_back_every  # seconds between "back to normal?" checks
        self.confirm_delay = confirm_delay      # seconds to wait before checking a new order
        self.max_failures = max_failures        # real failures (not post-only) before stopping
        self.order_price = None                 # where the current order really sits
        self.normal_price = None                # where it normally sits (BUY_PRICE / SELL_PRICE)
        self.next_check = None                  # time.monotonic() of the next move-back check
        # Part of the current side that filled before a stepped order was moved back:
        # {'amount', 'cost', 'fee_usd', 'fee_coin'}. Usually all zeros.
        self.parts = self.empty_parts()
        self.target = None                      # coins this side should fill in total
        self.done_with_parts = False            # moved back, rest too small: parts are the fill
        self.coin = pair.split('/')[0]          # 'USDG'
        self.quote = pair.split('/')[1]         # 'USD'
        # What the current buy ACTUALLY filled: {'amount', 'cost', 'fee_usd', 'sell_amount'}.
        # None until a buy fills. The sell and the profit use this, not self.amount.
        self.buy_fill = None
        self.label = f"{exchange.id} {pair}"     # 'kraken USDG/USD'
        # saved totals, so a restart doesn't start counting from 0 again
        self.total_profit, self.total_trades = load_totals(totals_file)

    def status_line(self, waiting_for, price, now):
        """One screen line per check, e.g.
        [kraken USDG/USD] 10:43:12 | waiting for buy @ 0.9999 | done: 3 | profit $0.0300
        and when the order had to step away from its normal price:
        ... | waiting for sell @ 1.0001  ⚠ STEPPED (normal 1.0) | next check for 1.0 at 12:58 | ..."""
        line = f"[{self.label}] {now:%H:%M:%S} | waiting for {waiting_for} @ {price}"
        if self.is_stepped():
            line += f"  ⚠ STEPPED (normal {self.normal_price})"
            if self.next_check is not None:
                left = max(0, self.next_check - time.monotonic())
                at = datetime.fromtimestamp(now.timestamp() + round(left))
                line += f" | next check for {self.normal_price} at {at:%H:%M}"
        return line + f" | done: {self.total_trades} | profit ${self.total_profit:.4f}"

    def is_stepped(self):
        return (self.order_price is not None and self.normal_price is not None
                and self.order_price != self.normal_price)

    @staticmethod
    def empty_parts():
        return {'amount': 0.0, 'cost': 0.0, 'fee_usd': 0.0, 'fee_coin': 0.0}

    def add_part(self, order):
        """Remember what a cancelled (moved-back) order had already filled."""
        filled = float(order.get('filled') or 0)
        if filled <= 0:
            return
        fee_usd, fee_coin = self.fee_parts(order)
        self.parts['amount'] += filled
        self.parts['cost'] += self.order_cost(order)
        self.parts['fee_usd'] += fee_usd
        self.parts['fee_coin'] += fee_coin

    def min_amount(self):
        """Smallest order the exchange takes (0 if it doesn't say)."""
        try:
            self.exchange.load_markets()
            return float(self.exchange.market(self.pair)['limits']['amount']['min'] or 0)
        except Exception:
            return 0.0

    def screen(self, message):
        print(f"[{self.label}] {datetime.now():%H:%M:%S} | {message}")
        
    def fee_parts(self, order):
        """(fee in USD, fee in coins) for a filled order. Kraken usually charges
        in USD; if it charged in coins, those coins are gone and can't be sold."""
        fee = order.get('fee')
        if not fee or fee.get('cost') is None:
            return 0.0, 0.0
        cost = float(fee['cost'])
        if fee.get('currency') == self.coin:
            price = float(order.get('average') or order.get('price'))
            return cost * price, cost
        return cost, 0.0

    def order_cost(self, order):
        """USD value of what actually filled.
        Kraken: from the order's trades (exact). Otherwise from the order."""
        exact = trades_cost(self.exchange, order, self.pair)
        if exact is not None:
            return exact
        if order.get('cost'):
            return float(order['cost'])
        price = float(order.get('average') or order.get('price'))
        return float(order['filled']) * price

    def buy_amount(self):
        """Coins to buy this cycle.
        compound off: always AMOUNT.
        compound on:  AMOUNT + total profit turned into coins at the buy price,
                      rounded DOWN to the exchange's precision.
                      e.g. AMOUNT 100, profit $3.00, buy 0.9999 -> 103.0003 coins.
        Only profit is added; a negative total never shrinks the buy below AMOUNT."""
        if not self.compound or self.total_profit <= 0:
            return self.amount
        extra = self.total_profit / self.buy_price
        self.exchange.load_markets()
        return float(self.exchange.amount_to_precision(self.pair, self.amount + extra))

    async def place_order(self, side):
        """Place the buy or the sell as a POST-ONLY limit order (never a taker,
        so never a taker fee).

        If Kraken rejects it because it would fill straight away as a taker
        (the market is already past our price), try again every check_interval
        (60 s), tries_per_price (5) times at that price, then step one price
        step further: a sell UP (1.0 -> 1.0001), a buy DOWN (0.9999 -> 0.9998).
        A stepped order is moved back by maybe_move_back() once the normal
        price is possible again (checked every hour).

        Other failures (no funds, network...) count separately; after
        max_failures (5) the bot stops and tells you."""
        normal = self.buy_price if side == 'buy' else self.sell_price
        steps, tries, failures = 0, 0, 0
        while True:
            price = step_price(normal, side, steps, self.step)
            try:
                amount = self.remaining_amount(side)
                order = self.exchange.create_order(self.pair, 'limit', side, amount, price,
                                                   {'postOnly': True})
                rejected = await self.rejected_after_placing(order)
            except Exception as e:
                if not is_post_only_error(e):
                    failures += 1
                    await self.send_alert(f"❌ {side.upper()} FAILED: {e}")
                    print(f"❌ {side.upper()} FAILED: {e}")
                    if failures >= self.max_failures:
                        raise OrderProblem(f"Could not place the {side} order after "
                                           f"{self.max_failures} attempts.")
                    print(f"{side} order failed (attempt {failures}/{self.max_failures}). "
                          f"Retrying in {self.check_interval}s...")
                    await asyncio.sleep(self.check_interval)
                    continue
                rejected = True

            if not rejected:
                await self.order_accepted(side, order, amount, price, normal)
                return order

            # post-only rejection: same price again, or one step further after 5 tries
            tries += 1
            if tries >= self.tries_per_price:
                shown = tries
                steps, tries = steps + 1, 0
                after = f"next try at {step_price(normal, side, steps, self.step)}"
            else:
                shown = tries
                after = f"next in {self.check_interval}s"
            self.screen(f"{side} @ {price} rejected (post-only: market already past it) - "
                        f"try {shown}/{self.tries_per_price}, {after}")
            await asyncio.sleep(self.check_interval)

    def remaining_amount(self, side):
        """Coins still to fill on this side. The first time: the full buy
        (AMOUNT + profit) or what the buy filled. After a moved-back order
        part-filled: only the rest, rounded DOWN."""
        if self.target is None:
            self.target = self.buy_amount() if side == 'buy' else self.sell_amount()
        if self.parts['amount'] <= 0:
            return self.target
        self.exchange.load_markets()
        return float(self.exchange.amount_to_precision(self.pair,
                                                       self.target - self.parts['amount']))

    async def rejected_after_placing(self, order):
        """Kraken can also accept a post-only order and cancel it straight away.
        Look once, a moment after placing: cancelled with nothing filled = rejected."""
        await asyncio.sleep(self.confirm_delay)
        check = self.exchange.fetch_order(order['id'], self.pair)
        return check['status'] in ('canceled', 'expired', 'rejected') and \
            float(check.get('filled') or 0) == 0

    async def order_accepted(self, side, order, amount, price, normal):
        """The order is on the book: remember it, save it, tell the user."""
        old_price = self.order_price
        moved_back = old_price is not None and old_price != normal and price == normal
        self.order_price = price
        self.normal_price = normal
        self.next_check = (time.monotonic() + self.move_back_every) if price != normal else None
        if side == 'buy':
            self.buy_order_id = order['id']
        else:
            self.sell_order_id = order['id']
        self.save_current_state(side)

        word = 'BUY' if side == 'buy' else 'SELL'
        dot = '🟢' if side == 'buy' else '🔴'
        if moved_back:
            await self.send_alert(f"↩️ {word} MOVED BACK to {price} (was {old_price})\n"
                                  f"{word.capitalize()} {amount} {self.coin} at ${price}")
        elif price != normal:
            direction = 'UP' if side == 'sell' else 'DOWN'
            await self.send_alert(f"⚠️ {word} STEPPED {direction} to {price} (normal {normal})\n"
                                  f"{word.capitalize()} {amount} {self.coin} at ${price}\n"
                                  f"{normal} would have filled as a taker. Checking every hour "
                                  f"to move back to {normal}.")
        else:
            await self.send_alert(f"{dot} {word} ORDER PLACED\n"
                                  f"{word.capitalize()} {amount} {self.coin} at ${price}")

    def save_current_state(self, side):
        order_id = self.buy_order_id if side == 'buy' else self.sell_order_id
        extra = {'price': self.order_price, 'normal': self.normal_price,
                 'parts': self.parts, 'target': self.target}
        save_state(side, order_id, self.state_file,
                   buy=self.buy_fill if side == 'sell' else None, extra=extra)

    async def maybe_move_back(self, side):
        """Once an hour, while an order is STEPPED: can it go back to its normal price?
        Sell: yes when the best bid is below 1.0 (a post-only sell at 1.0 would rest).
        Buy:  yes when the best ask is above 0.9999.
        If yes: cancel, keep anything that already filled, place the rest at normal
        (post-only; if that's rejected after all, the steps simply start again).
        Returns True if the order filled meanwhile (caller treats it as filled)."""
        if not self.is_stepped() or self.next_check is None:
            return False
        if time.monotonic() < self.next_check:
            return False
        self.next_check = time.monotonic() + self.move_back_every
        normal, price = self.normal_price, self.order_price
        ticker = self.exchange.fetch_ticker(self.pair)
        if side == 'sell':
            best, word = ticker.get('bid'), 'bid'
            can_move = best is not None and best < normal
        else:
            best, word = ticker.get('ask'), 'ask'
            can_move = best is not None and best > normal
        if not can_move:
            where = 'above' if side == 'sell' else 'below'
            self.screen(f"checking {normal}: best {word} {best}, still {where} - keeping {price}")
            return False

        self.screen(f"checking {normal}: best {word} {best} - moving {side} back to {normal}")
        order_id = self.buy_order_id if side == 'buy' else self.sell_order_id
        try:
            self.exchange.cancel_order(order_id, self.pair)
        except ccxt.OrderNotFound:
            pass                        # already gone: probably just filled
        order = self.exchange.fetch_order(order_id, self.pair)
        if order['status'] == 'closed':
            return True                 # it filled before the cancel: normal fill
        self.add_part(order)            # keep what filled before the cancel
        left = self.target - self.parts['amount']
        if self.parts['amount'] > 0 and left < self.min_amount():
            return True                 # the rest is too small to place: done with what filled
        await self.place_order(side)
        return False

    def fill_totals(self, order):
        """What this side filled in total: the final order plus any parts that
        filled before a stepped order was moved back. order=None: parts only."""
        amount, cost = self.parts['amount'], self.parts['cost']
        fee_usd, fee_coin = self.parts['fee_usd'], self.parts['fee_coin']
        if order is not None and float(order.get('filled') or 0) > 0:
            amount += float(order['filled'])
            cost += self.order_cost(order)
            o_fee_usd, o_fee_coin = self.fee_parts(order)
            fee_usd += o_fee_usd
            fee_coin += o_fee_coin
        return amount, cost, fee_usd, fee_coin

    def side_done(self):
        """Forget this side's order details; the next side starts at its normal price."""
        self.parts = self.empty_parts()
        self.target = None
        self.order_price = None
        self.normal_price = None
        self.next_check = None
        self.done_with_parts = False

    async def check_order(self, side):
        """Look at the current buy/sell order once.
        Returns the finished order (dict), 'parts' if only the parts count,
        or None if it's still waiting."""
        if self.done_with_parts:
            return 'parts'
        order_id = self.buy_order_id if side == 'buy' else self.sell_order_id
        order = self.exchange.fetch_order(order_id, self.pair)
        if order['status'] == 'closed':
            return order
        if order['status'] in ('canceled', 'expired', 'rejected'):
            reason = str((order.get('info') or {}).get('reason', '')).lower()
            if float(order.get('filled') or 0) == 0 and 'post' in reason:
                # post-only order cancelled by Kraken: place it again (steps if needed)
                await self.place_order(side)
                return None
            # The order is dead and will never fill. Stop instead of waiting forever.
            word = 'Buy' if side == 'buy' else 'Sell'
            raise OrderProblem(f"{word} order {order_id} was {order['status']}.")
        # still open: once an hour, see if a stepped order can go back to normal
        if await self.maybe_move_back(side):
            # filled just before the cancel, or the rest is too small to place
            order = self.exchange.fetch_order(order_id, self.pair)
            if order['status'] == 'closed':
                return order
            self.done_with_parts = True             # rest too small to place
            return 'parts'
        price = self.order_price if self.order_price is not None else \
            (self.buy_price if side == 'buy' else self.sell_price)
        print(self.status_line(side, price, datetime.now()))
        return None

    async def check_buy_filled(self):
        """True once the buy has filled (also: parts + final order after a move-back)."""
        if not self.buy_order_id:
            return False
        try:
            order = await self.check_order('buy')
            if order is None:
                return False
            final = None if order == 'parts' else order
            filled_amount, cost, fee_usd, fee_coin = self.fill_totals(final)
            avg_price = round(cost / filled_amount, 8) if filled_amount else self.buy_price
            sell_amount = filled_amount
            if fee_coin > 0:
                # Fee came out of the coins: sell only what we really have,
                # rounded DOWN to the exchange's precision.
                self.exchange.load_markets()
                sell_amount = float(self.exchange.amount_to_precision(
                    self.pair, filled_amount - fee_coin))
            self.buy_fill = {'amount': filled_amount, 'cost': cost,
                             'fee_usd': fee_usd, 'sell_amount': sell_amount}
            self.side_done()
            message = (f"✅ BUY FILLED!\nFilled: {filled_amount} {self.coin}\n"
                       f"Average price: ${avg_price}\n"
                       f"Now placing SELL order at ${self.sell_price}...")
            log_trade('buy', avg_price, filled_amount, self.buy_order_id)
            await self.send_alert(message)
            print(f"Buy order filled at {avg_price}")
            return True
        except OrderProblem:
            raise               # let run_continuous stop the bot
        except Exception as e:
            print(f"Error checking buy order: {e}")
            return False

    def sell_amount(self):
        """Sell exactly what the buy filled (not AMOUNT, which may have changed)."""
        if self.buy_fill is None:
            return self.amount          # only if we never saw the buy fill
        return self.buy_fill['sell_amount']

    async def place_sell_order(self):
        """
        Place a sell order at self.sell_price
        """
        try:
            order = self.exchange.create_limit_sell_order(
                self.pair,
                self.sell_amount(),
                self.sell_price
            )
            self.sell_order_id = order['id']
            
        except Exception as e:
            error_message = f"❌ SELL FAILED: {str(e)}"
            await self.send_alert(error_message)
            print(error_message)
            return None

        save_state('sell', self.sell_order_id, self.state_file, buy=self.buy_fill)
        message = f"🔴 SELL ORDER PLACED\nSell {self.sell_amount()} {self.coin} at ${self.sell_price}"
        await self.send_alert(message)
        return order
    
    async def check_sell_filled(self):
        """True once the sell has filled; adds the profit to the totals."""
        if not self.sell_order_id:
            return False
        try:
            order = await self.check_order('sell')
            if order is None:
                return False
            final = None if order == 'parts' else order
            filled_amount, revenue, sell_fee_usd, _ = self.fill_totals(final)
            avg_price = round(revenue / filled_amount, 8) if filled_amount else self.sell_price

            # Profit from what really happened: sell revenue - buy cost - both fees
            if self.buy_fill is not None:
                buy_cost = self.buy_fill['cost'] + self.buy_fill['fee_usd']
            else:
                # state saved before this fix: assume we bought what we sold
                buy_cost = filled_amount * self.buy_price
            profit = revenue - buy_cost - sell_fee_usd

            self.total_profit += profit
            self.total_trades += 1
            save_totals(self.total_profit, self.total_trades, self.totals_file)
            self.side_done()

            log_trade('sell', avg_price, filled_amount, self.sell_order_id)
            message = (f"✅ SELL FILLED!\nSold: {filled_amount} {self.coin}\n"
                       f"Average price: ${avg_price}\n💰 Profit: ${profit:.4f}\n"
                       f"📊 Total: {self.total_trades} trades | ${self.total_profit:.4f}")
            await self.send_alert(message)
            print(f"Sell order filled at {avg_price}. Profit: ${profit:.4f}")
            return True
        except OrderProblem:
            raise
        except Exception as e:
            print(f"Error checking sell order: {e}")
            return False

    async def recover(self):
        existing_trade = load_state(self.state_file)
        if existing_trade is None:
            return
        if existing_trade.get('price') is not None:
            # where the order really sits (maybe STEPPED), and any parts already filled
            self.order_price = existing_trade['price']
            self.normal_price = existing_trade['normal']
            self.parts = existing_trade.get('parts') or self.empty_parts()
            self.target = existing_trade.get('target')
            if self.is_stepped():
                self.next_check = time.monotonic() + self.move_back_every
        if existing_trade['waiting_for'] == 'buy':
            self.buy_order_id = existing_trade['order_id']
            message = f"🔄 BOT RESTARTED - recovered buy order {self.buy_order_id}."
            await self.send_alert(message)
        elif existing_trade['waiting_for'] == 'sell':
            self.sell_order_id = existing_trade['order_id']
            self.buy_fill = existing_trade.get('buy')     # None in old state files
            message = f"🔄 BOT RESTARTED - recovered sell order {self.sell_order_id}."
            await self.send_alert(message)
    
    async def run_cycle(self):
        """
        Execute one complete buy-sell cycle
        """
        if self.sell_order_id is None:
            if self.buy_order_id is None:
                print(f"\n{'='*50}")
                print(f"Starting new cycle (Total trades: {self.total_trades})")
                print(f"{'='*50}")
                
                # Place buy order (post-only; steps if rejected; stops after 5 real failures)
                await self.place_order('buy')

            # Wait for buy to fill
            print(f"Waiting for buy to fill (checking every {self.check_interval}s)...")
            while not await self.check_buy_filled():
                # KEY: await asyncio.sleep() instead of time.sleep()
                # time.sleep(60) = BLOCKS everything for 60 sec
                # await asyncio.sleep(60) = Non-blocking. Other async tasks can run!
                await asyncio.sleep(self.check_interval)
            
            # Buy filled, place sell (you now hold the coins, so this must not silently fail)
            print("Buy filled! Placing sell order...")
            await self.place_order('sell')
        
        # Wait for sell to fill
        print(f"Waiting for sell to fill (checking every {self.check_interval}s)...")
        while not await self.check_sell_filled():
            await asyncio.sleep(self.check_interval)
        
        # Reset for next cycle
        print("Cycle complete!")
        self.buy_order_id = None
        self.sell_order_id = None
        self.buy_fill = None
        self.side_done()

        save_state(None, None, self.state_file)
    
    async def run_continuous(self):
        """
        Run the bot continuously
        """

        await self.recover()

        cycle_count = 0
        try:
            while True:
                cycle_count += 1
                await self.run_cycle()
                print(f"Waiting for next opportunity... (Completed {cycle_count} cycles)")
        except OrderProblem as e:
            # An order failed or was cancelled. Tell the user and stop.
            message = f"🛑 BOT STOPPED - order problem\n{e}\nCheck Kraken (open orders and balances) before restarting."
            await self.send_alert(message)
            print(f"\n{message}")
        except asyncio.CancelledError:
            # Ctrl+C: asyncio.run() cancels the running task, which lands here.
            final_message = f"⏹️ BOT STOPPED\nTotal cycles: {cycle_count}\nTotal profit: ${self.total_profit:.4f}"
            await self.send_alert(final_message)
            print(f"\n{final_message}")
            raise  # re-raise so the program can exit cleanly
    
    async def send_alert(self, message):
        """
        Send message to Telegram
        """
        async def send(text):
            await self.telegram_bot.send_message(chat_id=self.chat_id, text=text)

        # up to 3 tries on time-outs/network problems, never raises
        if await send_with_retries(send, message):
            print(f"[TELEGRAM SENT] {message}")
        else:
            print(f"[MESSAGE] {message}")