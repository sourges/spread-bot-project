import asyncio
from datetime import datetime
from telegram import Bot
from log_trade import log_trade
from state import save_state, load_state, save_totals, load_totals


class OrderProblem(Exception):
    """Raised when an order is cancelled/expired/rejected, or can't be placed.
    The bot stops and tells you, instead of waiting forever."""


class SpreadBot:
    def __init__(self, exchange, pair, buy_price, sell_price, amount, 
                 telegram_token, chat_id, check_interval=60, totals_file='spread_totals.json',
                 state_file='state.json', compound=False):
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
        [kraken USDG/USD] 10:43:12 | waiting for buy @ 0.9999 | done: 3 | profit $0.0300"""
        return (f"[{self.label}] {now:%H:%M:%S} | waiting for {waiting_for} @ {price} | "
                f"done: {self.total_trades} | profit ${self.total_profit:.4f}")
        
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
        """USD value of what actually filled."""
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

    async def place_buy_order(self):
        """
        Place a buy order at self.buy_price
        """
        try:
            amount = self.buy_amount()      # inside try: a network error here retries like a failed order
            order = self.exchange.create_limit_buy_order(
                self.pair,
                amount,
                self.buy_price
            )
            self.buy_order_id = order['id']
            
        except Exception as e:
            error_message = f"❌ BUY FAILED: {str(e)}"
            await self.send_alert(error_message)
            print(error_message)
            return None

        save_state('buy', self.buy_order_id, self.state_file)
        message = f"🟢 BUY ORDER PLACED\nBuy {amount} {self.coin} at ${self.buy_price}"
        await self.send_alert(message)
        return order
    
    async def check_buy_filled(self):
        """
        Check if buy order has been filled
        
        FIXED: Fallback for average field (sometimes None from CCXT)
        """
        if not self.buy_order_id:
            return False
            
        try:
            order = self.exchange.fetch_order(self.buy_order_id, self.pair)
            
            if order['status'] == 'closed':
                # FIXED: Fallback chain if average is None or missing
                avg_price = order.get('average') or order.get('price') or self.buy_price
                filled_amount = float(order['filled'])
                fee_usd, fee_coin = self.fee_parts(order)
                sell_amount = filled_amount
                if fee_coin > 0:
                    # Fee came out of the coins: sell only what we really have,
                    # rounded DOWN to the exchange's precision.
                    self.exchange.load_markets()
                    sell_amount = float(self.exchange.amount_to_precision(
                        self.pair, filled_amount - fee_coin))
                self.buy_fill = {'amount': filled_amount, 'cost': self.order_cost(order),
                                 'fee_usd': fee_usd, 'sell_amount': sell_amount}
                message = f"✅ BUY FILLED!\nFilled: {filled_amount} {self.pair.split('/')[0]}\nAverage price: ${avg_price}\nNow placing SELL order at ${self.sell_price}..."

                # save trade to log
                log_trade('buy', avg_price, filled_amount, self.buy_order_id)

                await self.send_alert(message)
                print(f"Buy order filled at {avg_price}")
                return True
            elif order['status'] in ('canceled', 'expired', 'rejected'):
                # The order is dead and will never fill. Stop instead of waiting forever.
                raise OrderProblem(f"Buy order {self.buy_order_id} was {order['status']}.")
            else:
                print(self.status_line('buy', self.buy_price, datetime.now()))
                return False
        except OrderProblem:
            # Let this one pass through so run_continuous can stop the bot.
            raise
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
        """
        Check if sell order has been filled
        
        FIXED: Fallback for average field (sometimes None from CCXT)
        """
        if not self.sell_order_id:
            return False
            
        try:
            order = self.exchange.fetch_order(self.sell_order_id, self.pair)
            
            if order['status'] == 'closed':
                # FIXED: Fallback chain if average is None or missing
                avg_price = order.get('average') or order.get('price') or self.sell_price
                filled_amount = float(order['filled'])

                # Profit from what really happened: sell revenue - buy cost - both fees
                revenue = self.order_cost(order)
                sell_fee_usd, _ = self.fee_parts(order)
                if self.buy_fill is not None:
                    buy_cost = self.buy_fill['cost'] + self.buy_fill['fee_usd']
                else:
                    # state saved before this fix: assume we bought what we sold
                    buy_cost = filled_amount * self.buy_price
                profit = revenue - buy_cost - sell_fee_usd
                
                self.total_profit += profit
                self.total_trades += 1
                save_totals(self.total_profit, self.total_trades, self.totals_file)

                # save trade to log
                log_trade('sell', avg_price, filled_amount, self.sell_order_id)
                
                message = f"✅ SELL FILLED!\nSold: {filled_amount} {self.pair.split('/')[0]}\nAverage price: ${avg_price}\n💰 Profit: ${profit:.4f}\n📊 Total: {self.total_trades} trades | ${self.total_profit:.4f}"
                await self.send_alert(message)
                print(f"Sell order filled at {avg_price}. Profit: ${profit:.4f}")
                return True
            elif order['status'] in ('canceled', 'expired', 'rejected'):
                # The order is dead and will never fill. Stop instead of waiting forever.
                raise OrderProblem(f"Sell order {self.sell_order_id} was {order['status']}.")
            else:
                print(self.status_line('sell', self.sell_price, datetime.now()))
                return False
        except OrderProblem:
            # Let this one pass through so run_continuous can stop the bot.
            raise
        except Exception as e:
            print(f"Error checking sell order: {e}")
            return False
    
    async def place_with_retries(self, place_func, what, max_attempts=5):
        """
        Try to place an order up to max_attempts times.
        place_func is place_buy_order or place_sell_order; they return None on failure.
        If every attempt fails, raise OrderProblem so the bot stops and tells you.
        """
        for attempt in range(1, max_attempts + 1):
            order = await place_func()
            if order is not None:
                return order
            if attempt < max_attempts:
                print(f"{what} failed (attempt {attempt}/{max_attempts}). Retrying in {self.check_interval}s...")
                await asyncio.sleep(self.check_interval)
        raise OrderProblem(f"Could not place the {what} after {max_attempts} attempts.")

    async def recover(self):
        existing_trade = load_state(self.state_file)
        if existing_trade is None:
            return
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
                
                # Place buy order (retries a few times; stops the bot if it never works)
                await self.place_with_retries(self.place_buy_order, "buy order")

            # Wait for buy to fill
            print(f"Waiting for buy to fill (checking every {self.check_interval}s)...")
            while not await self.check_buy_filled():
                # KEY: await asyncio.sleep() instead of time.sleep()
                # time.sleep(60) = BLOCKS everything for 60 sec
                # await asyncio.sleep(60) = Non-blocking. Other async tasks can run!
                await asyncio.sleep(self.check_interval)
            
            # Buy filled, place sell (you now hold the coins, so this must not silently fail)
            print("Buy filled! Placing sell order...")
            await self.place_with_retries(self.place_sell_order, "sell order")
        
        # Wait for sell to fill
        print(f"Waiting for sell to fill (checking every {self.check_interval}s)...")
        while not await self.check_sell_filled():
            await asyncio.sleep(self.check_interval)
        
        # Reset for next cycle
        print("Cycle complete!")
        self.buy_order_id = None
        self.sell_order_id = None
        self.buy_fill = None

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
        try:
            # Direct await - clean and simple!
            await self.telegram_bot.send_message(
                chat_id=self.chat_id, 
                text=message
            )
            print(f"[TELEGRAM SENT] {message}")
        except Exception as e:
            print(f"[TELEGRAM ERROR] Failed to send: {e}")
            print(f"[MESSAGE] {message}")