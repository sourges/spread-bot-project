import asyncio
from telegram import Bot

class SpreadBot:
    def __init__(self, exchange, pair, buy_price, sell_price, amount, 
                 telegram_token, chat_id, check_interval=60):
        """
        Initialize the spread bot
        
        Note: __init__ does NOT need to be async - it just sets up data
        Async is only for functions that WAIT for something
        """
        self.exchange = exchange
        self.pair = pair
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.amount = amount
        self.telegram_bot = Bot(token=telegram_token)
        self.chat_id = chat_id
        self.check_interval = check_interval
        
        self.buy_order_id = None
        self.sell_order_id = None
        self.total_profit = 0
        self.total_trades = 0
        
    async def place_buy_order(self):
        """
        Place a buy order at self.buy_price
        
        WHY async? - We call await self.send_alert() inside
        """
        try:
            order = self.exchange.create_limit_buy_order(
                self.pair,
                self.amount,
                self.buy_price
            )
            self.buy_order_id = order['id']
            message = f"🟢 BUY ORDER PLACED\nBuy {self.amount} {self.pair.split('/')[0]} at ${self.buy_price}"
            
            await self.send_alert(message)
            return order
        except Exception as e:
            error_message = f"❌ BUY FAILED: {str(e)}"
            await self.send_alert(error_message)
            print(error_message)
            return None
    
    async def check_buy_filled(self):
        """
        Check if buy order has been filled
        
        WHY async? - Calls await self.send_alert()
        
        FIXED: Fallback for average field (sometimes None from CCXT)
        """
        if not self.buy_order_id:
            return False
            
        try:
            order = self.exchange.fetch_order(self.buy_order_id, self.pair)
            
            if order['status'] == 'closed':
                # FIXED: Fallback chain if average is None or missing
                avg_price = order.get('average') or order.get('price') or self.buy_price
                filled_amount = order['filled']
                message = f"✅ BUY FILLED!\nFilled: {filled_amount} {self.pair.split('/')[0]}\nAverage price: ${avg_price}\nNow placing SELL order at ${self.sell_price}..."
                
                await self.send_alert(message)
                print(f"Buy order filled at {avg_price}")
                return True
            else:
                print(f"Buy order still open... Status: {order['status']}")
                return False
        except Exception as e:
            print(f"Error checking buy order: {e}")
            return False
    
    async def place_sell_order(self):
        """
        Place a sell order at self.sell_price
        
        WHY async? - Calls await self.send_alert()
        """
        try:
            order = self.exchange.create_limit_sell_order(
                self.pair,
                self.amount,
                self.sell_price
            )
            self.sell_order_id = order['id']
            message = f"🔴 SELL ORDER PLACED\nSell {self.amount} {self.pair.split('/')[0]} at ${self.sell_price}"
            await self.send_alert(message)
            return order
        except Exception as e:
            error_message = f"❌ SELL FAILED: {str(e)}"
            await self.send_alert(error_message)
            print(error_message)
            return None
    
    async def check_sell_filled(self):
        """
        Check if sell order has been filled
        
        WHY async? - Calls await self.send_alert()
        
        FIXED: Fallback for average field (sometimes None from CCXT)
        """
        if not self.sell_order_id:
            return False
            
        try:
            order = self.exchange.fetch_order(self.sell_order_id, self.pair)
            
            if order['status'] == 'closed':
                # FIXED: Fallback chain if average is None or missing
                avg_price = order.get('average') or order.get('price') or self.sell_price
                filled_amount = order['filled']
                
                # Calculate profit
                cost_basis = self.amount * self.buy_price
                revenue = filled_amount * avg_price
                profit = revenue - cost_basis
                
                self.total_profit += profit
                self.total_trades += 1
                
                message = f"✅ SELL FILLED!\nSold: {filled_amount} {self.pair.split('/')[0]}\nAverage price: ${avg_price}\n💰 Profit: ${profit:.4f}\n📊 Total: {self.total_trades} trades | ${self.total_profit:.4f}"
                await self.send_alert(message)
                print(f"Sell order filled at {avg_price}. Profit: ${profit:.4f}")
                return True
            else:
                print(f"Sell order still open... Status: {order['status']}")
                return False
        except Exception as e:
            print(f"Error checking sell order: {e}")
            return False
    
    async def run_cycle(self):
        """
        Execute one complete buy-sell cycle
        
        WHY async?
        - We use await asyncio.sleep() instead of time.sleep()
        - This is the MAGIC of async: while we sleep, we don't block
        - If running multiple bots, they could run during this sleep
        """
        print(f"\n{'='*50}")
        print(f"Starting new cycle (Total trades: {self.total_trades})")
        print(f"{'='*50}")
        
        # Place buy order
        await self.place_buy_order()
        
        # Wait for buy to fill
        print(f"Waiting for buy to fill (checking every {self.check_interval}s)...")
        while not await self.check_buy_filled():
            # KEY: await asyncio.sleep() instead of time.sleep()
            # time.sleep(60) = BLOCKS everything for 60 sec
            # await asyncio.sleep(60) = Non-blocking. Other async tasks can run!
            await asyncio.sleep(self.check_interval)
        
        # Buy filled, place sell
        print("Buy filled! Placing sell order...")
        await self.place_sell_order()
        
        # Wait for sell to fill
        print(f"Waiting for sell to fill (checking every {self.check_interval}s)...")
        while not await self.check_sell_filled():
            await asyncio.sleep(self.check_interval)
        
        # Reset for next cycle
        print("Cycle complete!")
        self.buy_order_id = None
        self.sell_order_id = None
    
    async def run_continuous(self):
        """
        Run the bot continuously
        
        WHY async?
        - Calls await self.run_cycle()
        - The entire bot loop is async
        """
        cycle_count = 0
        try:
            while True:
                cycle_count += 1
                await self.run_cycle()
                print(f"Waiting for next opportunity... (Completed {cycle_count} cycles)")
        except KeyboardInterrupt:
            # User pressed Ctrl+C
            final_message = f"⏹️ BOT STOPPED\nTotal cycles: {cycle_count}\nTotal profit: ${self.total_profit:.4f}"
            await self.send_alert(final_message)
            print(f"\n{final_message}")
    
    async def send_alert(self, message):
        """
        Send message to Telegram
        
        WHY async?
        - Telegram API call is asynchronous (takes time over the network)
        - Using async/await means other code can run while waiting
        - Much cleaner than: asyncio.run(telegram_call)
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
