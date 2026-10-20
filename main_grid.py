import asyncio
import os
import ccxt
from dotenv import load_dotenv
from telegram import Bot
from grid import geometric_levels, tick_to_decimals, plan_entries, order_amount, check_config
from grid_bot import GridBot, run_bots
from bots_config import BOTS, CHECK_INTERVAL, DRY_RUN

load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def to_decimals(exchange, precision):
    """ccxt gives precision as a tick size (0.001) or as decimals (3),
    depending on the exchange. Always return decimals."""
    if exchange.precisionMode == ccxt.TICK_SIZE:
        return tick_to_decimals(precision)
    return int(precision)


def make_exchange(name):
    """One connection per exchange, shared by all its bots, so ccxt can
    pace requests across them and stay under the account's rate limit.
    Keys come from .env as KRAKEN_API_KEY / KRAKEN_API_SECRET, etc."""
    key = os.getenv(f'{name.upper()}_API_KEY')
    secret = os.getenv(f'{name.upper()}_API_SECRET')
    if not key or not secret:
        raise ValueError(f"Missing {name.upper()}_API_KEY or {name.upper()}_API_SECRET in .env")
    exchange = getattr(ccxt, name)({'apiKey': key, 'secret': secret})
    exchange.load_markets()
    return exchange


def build_bot(exchange, cfg, alert):
    """Work out the grid for one bot, print what it would do, return the GridBot."""
    pair = cfg['pair']
    market = exchange.market(pair)
    price_decimals = to_decimals(exchange, market['precision']['price'])
    amount_decimals = to_decimals(exchange, market['precision']['amount'])
    min_amount = market['limits']['amount']['min']

    levels = geometric_levels(cfg['lower'], cfg['upper'], cfg['levels'], price_decimals)
    if len(set(levels)) != len(levels):
        raise ValueError(f"{pair}: some levels are identical after rounding. Use fewer levels.")

    price = exchange.fetch_ticker(pair)['last']
    print(f"\n=== {exchange.id} {pair} ===")
    print(f"Price {price} | {len(levels)} levels {levels[0]}–{levels[-1]} | "
          f"min amount {min_amount}")
    plan = plan_entries(levels, price, [], cfg['count'])
    for t in plan['place']:
        side = 'BUY ' if t['side'] == 'long' else 'SELL'
        amount = order_amount(cfg['usd_per_entry'], t['entry_price'], amount_decimals, min_amount)
        print(f"  {side} {amount} @ {t['entry_price']}  (exit @ {t['exit_price']})")

    return GridBot(exchange, pair, levels, cfg['usd_per_entry'], amount_decimals,
                   min_amount, alert, count=cfg['count'], check_interval=CHECK_INTERVAL)


async def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return

    # v2 renamed the files to grid_state_kraken_CRV_USD.json etc. If the old
    # file is still here, starting would forget the open trades, so stop.
    if os.path.exists('grid_state.json'):
        print("❌ Found the old grid_state.json. Rename it before starting:\n"
              "   grid_state.json  -> grid_state_kraken_CRV_USD.json\n"
              "   grid_trades.txt  -> grid_trades_kraken_CRV_USD.txt")
        return

    try:
        check_config(BOTS)
    except ValueError as e:
        print(f"❌ bots_config.py: {e}")
        return

    telegram = Bot(token=TELEGRAM_TOKEN)

    async def alert(message):
        print(message)
        try:
            await telegram.send_message(chat_id=CHAT_ID, text=message)
        except Exception as e:
            print(f"[TELEGRAM ERROR] {e}")

    exchanges = {}
    bots = []
    for cfg in BOTS:
        name = cfg['exchange']
        if name not in exchanges:
            exchanges[name] = make_exchange(name)
        bots.append(build_bot(exchanges[name], cfg, alert))

    if DRY_RUN:
        print("\nDRY_RUN is on. Nothing was placed. Set DRY_RUN = False in bots_config.py to trade.")
        return

    print(f"\n🚀 Running {len(bots)} grid bot(s). Ctrl+C to stop all.")
    await run_bots(bots)


if __name__ == '__main__':
    asyncio.run(main())
