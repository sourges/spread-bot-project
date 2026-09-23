from dotenv import load_dotenv
import os
import ccxt
from spread_bot import SpreadBot
import asyncio

# Load environment variables from .env file
load_dotenv()

# Get credentials from .env
KRAKEN_KEY = os.getenv('KRAKEN_API_KEY')
KRAKEN_SECRET = os.getenv('KRAKEN_API_SECRET')
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
        print("  - KRAKEN_API_KEY")
        print("  - KRAKEN_API_SECRET")
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
    print("\nFetching USDG/USD price...")
    try:
        ticker = exchange.fetch_ticker('USDG/USD')
        current_price = ticker['last']
        print(f"✅ Current USDG/USD price: ${current_price}")
    except Exception as e:
        print(f"❌ Failed to fetch price: {e}")
        exit()

    print("\n" + "="*50)
    print("ALL TESTS PASSED - BOT READY TO RUN")
    print("="*50)

    # Create SpreadBot instance
    bot = SpreadBot(
        exchange=exchange,
        pair='USDG/USD',
        buy_price=0.9999,
        sell_price=1.0001,
        amount=10,
        telegram_token=TELEGRAM_TOKEN,
        chat_id=CHAT_ID,
        check_interval=60
    )

    # Ask user before running
    print("\n⚠️  WARNING: This will place REAL trades on Kraken!")
    print(f"   Trading pair: USDG/USD")
    print(f"   Buy price: 0.9999")
    print(f"   Sell price: 1.0001")
    print(f"   Amount per trade: 10 USDG")
    response = input("\nDo you want to start the bot? (yes/no): ")

    if response.lower() == 'yes':
        print("\n🚀 Starting SpreadBot...")
        print("(Press Ctrl+C to stop the bot)\n")
        await bot.run_continuous()
    else:
        print("Bot cancelled. Exiting.")
        exit()

if __name__ == "__main__":
    asyncio.run(main())