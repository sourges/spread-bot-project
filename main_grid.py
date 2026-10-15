import asyncio
import os
import ccxt
from dotenv import load_dotenv
from telegram import Bot
from grid import geometric_levels, tick_to_decimals, plan_entries, order_amount
from grid_bot import GridBot

# ---------- settings ----------
PAIR = 'CRV/USD'
LOWER = 0.1431          # same range as the 3commas CRV bot
UPPER = 1.1442
NUM_LEVELS = 71         # 70 grids → ~3.01% apart
USD_PER_ENTRY = 61
COUNT = 2               # 2 buys below + 2 sells above
CHECK_INTERVAL = 60

# DRY_RUN = True: connect, show the plan, place NOTHING.
# Set to False only after the dry run looks right.
DRY_RUN = True
# ------------------------------

load_dotenv()
KRAKEN_KEY = os.getenv('KRAKEN_API_KEY')
KRAKEN_SECRET = os.getenv('KRAKEN_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def to_decimals(exchange, precision):
    """ccxt gives precision as a tick size (0.001) or as decimals (3),
    depending on the exchange. Always return decimals."""
    if exchange.precisionMode == ccxt.TICK_SIZE:
        return tick_to_decimals(precision)
    return int(precision)


async def main():
    if not all([KRAKEN_KEY, KRAKEN_SECRET, TELEGRAM_TOKEN, CHAT_ID]):
        print("❌ Missing credentials in .env")
        return

    exchange = ccxt.kraken({'apiKey': KRAKEN_KEY, 'secret': KRAKEN_SECRET})
    exchange.load_markets()
    market = exchange.market(PAIR)

    price_decimals = to_decimals(exchange, market['precision']['price'])
    amount_decimals = to_decimals(exchange, market['precision']['amount'])
    min_amount = market['limits']['amount']['min']

    levels = geometric_levels(LOWER, UPPER, NUM_LEVELS, price_decimals)
    if len(set(levels)) != len(levels):
        print("❌ Some levels are identical after rounding. Use fewer levels.")
        return

    price = exchange.fetch_ticker(PAIR)['last']
    print(f"{PAIR} price: {price}")
    print(f"Price decimals: {price_decimals} | amount decimals: {amount_decimals} | min amount: {min_amount}")
    print(f"{len(levels)} levels from {levels[0]} to {levels[-1]}")

    # Show what the first cycle would place (assumes no saved trades)
    plan = plan_entries(levels, price, [], COUNT)
    print("\nFirst cycle would place:")
    for t in plan['place']:
        side = 'BUY ' if t['side'] == 'long' else 'SELL'
        amount = order_amount(USD_PER_ENTRY, t['entry_price'], amount_decimals, min_amount)
        print(f"  {side} {amount} @ {t['entry_price']}  (exit @ {t['exit_price']})")

    if DRY_RUN:
        print("\nDRY_RUN is on. Nothing was placed. Set DRY_RUN = False to trade.")
        return

    telegram = Bot(token=TELEGRAM_TOKEN)

    async def alert(message):
        print(message)
        try:
            await telegram.send_message(chat_id=CHAT_ID, text=message)
        except Exception as e:
            print(f"[TELEGRAM ERROR] {e}")

    bot = GridBot(exchange, PAIR, levels, USD_PER_ENTRY, amount_decimals, min_amount,
                  alert, count=COUNT, check_interval=CHECK_INTERVAL)
    print("\n🚀 Grid bot running. Ctrl+C to stop.")
    await bot.run_continuous()


if __name__ == '__main__':
    asyncio.run(main())
