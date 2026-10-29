from dotenv import load_dotenv
import os
import ccxt
from spread_bot import SpreadBot
import asyncio

# Load environment variables from .env file
load_dotenv()

# ===== SETTINGS (the only place to change them) =====
PAIR = 'USDG/USD'
BUY_PRICE = 0.9999
SELL_PRICE = 1.0000
AMOUNT = 5000            # coins per trade
COMPOUND = True         # True: add total profit to each new buy. False: always AMOUNT.
CHECK_INTERVAL = 60     # seconds between checks
# =====================================================

# Get credentials from .env (spread bot has its own Kraken key)
KRAKEN_KEY = os.getenv('KRAKEN_SPREAD_API_KEY')
KRAKEN_SECRET = os.getenv('KRAKEN_SPREAD_API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

async def main():
    """Main async function"""
    
    print("="*50)
    print("SPREAD BOT INITIALIZING")
    print("="*50)

    # Check if credentials exist
    if not all([KRAKEN_KEY, KRAKEN_SECRET, TELEGRAM_TOKEN, CHAT_ID]):
        print("❌ ERROR: Missing credentials in .env file!")
        print("Make sure you have:")
        print("  - KRAKEN_SPREAD_API_KEY")
        print("  - KRAKEN_SPREAD_API_SECRET")
        print("  - TELEGRAM_BOT_TOKEN")
        print("  - TELEGRAM_CHAT_ID")
        exit()

    print("✅ Credentials loaded from .env")

    # Create Kraken exchange connection
    print("Connecting to Kraken...")
    kraken_config = {
        'apiKey': KRAKEN_KEY,
        'secret': KRAKEN_SECRET
    }

    try:
        exchange = ccxt.kraken(kraken_config)
        print("✅ Connected to Kraken!")
    except Exception as e:
        print(f"❌ Failed to connect to Kraken: {e}")
        exit()

    # Test by fetching balance
    try:
        balance = exchange.fetch_balance()
        print("\n📊 Account Balance:")
        for currency in balance['free']:
            if balance['free'][currency] > 0:
                print(f"  {currency}: {balance['free'][currency]}")
    except Exception as e:
        print(f"❌ Failed to fetch balance: {e}")
        exit()

    # Test by fetching current price
    print(f"\nFetching {PAIR} price...")
    try:
        ticker = exchange.fetch_ticker(PAIR)
        current_price = ticker['last']
        print(f"✅ Current {PAIR} price: ${current_price}")
    except Exception as e:
        print(f"❌ Failed to fetch price: {e}")
        exit()

    print("\n" + "="*50)
    print("ALL TESTS PASSED - BOT READY TO RUN")
    print("="*50)

    # Create SpreadBot instance
    bot = SpreadBot(
        exchange=exchange,
        pair=PAIR,
        buy_price=BUY_PRICE,
        sell_price=SELL_PRICE,
        amount=AMOUNT,
        telegram_token=TELEGRAM_TOKEN,
        chat_id=CHAT_ID,
        check_interval=CHECK_INTERVAL,
        compound=COMPOUND
    )

    coin = PAIR.split('/')[0]
    print("\n⚠️  LIVE: placing REAL trades on Kraken")
    print(f"   Trading pair: {PAIR}")
    print(f"   Buy price: {BUY_PRICE}")
    print(f"   Sell price: {SELL_PRICE}")
    print(f"   Amount per trade: {AMOUNT} {coin}")
    if COMPOUND:
        print(f"   Compound: ON (next buy {bot.buy_amount()} {coin}, "
              f"AMOUNT + ${bot.total_profit:.4f} profit)")
    else:
        print("   Compound: OFF")
    print("\n🚀 Starting SpreadBot...")
    print("(Press Ctrl+C to stop the bot)\n")
    await bot.run_continuous()

if __name__ == "__main__":
    asyncio.run(main())