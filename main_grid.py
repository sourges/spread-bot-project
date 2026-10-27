import asyncio
import os
import ccxt
from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest
from grid import geometric_levels, tick_to_decimals, check_config, preview_lines
from grid_state import load_grid_state
from grid_bot import GridBot, run_bots, print_status, daily_summary, check_funding
from bots_config import BOTS, CHECK_INTERVAL, DRY_RUN, SUMMARY_HOUR

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

    bot = GridBot(exchange, pair, levels, cfg['usd_per_entry'], amount_decimals,
                  min_amount, alert, count=cfg['count'], check_interval=CHECK_INTERVAL)

    # Preview from the SAVED trades and levels (what recover() will use),
    # so after a restart it shows what's really open, not a fresh grid.
    state = load_grid_state(bot.state_file)
    trades = state['trades'] if state else []
    price = exchange.fetch_ticker(pair)['last']
    print(f"\n=== {exchange.id} {pair} ===")
    if state and state['levels'] != levels:
        print("  (settings changed: using the SAVED levels so open trades still match)")
        levels = state['levels']
    print(f"Price {price} | {len(levels)} levels {levels[0]}–{levels[-1]} | "
          f"min amount {min_amount} | saved trades: {len(trades)}")
    lines = preview_lines(levels, price, trades, cfg['count'], cfg['usd_per_entry'],
                          amount_decimals, min_amount)
    for line in lines:
        print(line)
    if any('below min amount' in line for line in lines):
        # same as before: a size the exchange won't accept stops startup, it doesn't fail quietly
        raise ValueError(f"{pair}: usd_per_entry ${cfg['usd_per_entry']} is below the "
                         f"exchange's minimum order ({min_amount}). Raise it in bots_config.py.")

    return bot


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

    # ccxt calls pause the whole program for a moment, so give Telegram
    # 30 s instead of the default 5 s before calling it "timed out".
    telegram = Bot(token=TELEGRAM_TOKEN,
                   request=HTTPXRequest(connect_timeout=30, read_timeout=30,
                                        write_timeout=30, pool_timeout=30))

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

    await check_funding(bots, alert)

    if DRY_RUN:
        print("\nDRY_RUN is on. Nothing was placed. Set DRY_RUN = False in bots_config.py to trade.")
        return

    print(f"\n🚀 Running {len(bots)} grid bot(s). Ctrl+C to stop all.")
    print(f"   Daily summary at {SUMMARY_HOUR}:00 (this PC's clock).")
    await run_bots(bots, helpers=[print_status(bots, CHECK_INTERVAL),
                                  daily_summary(bots, alert, SUMMARY_HOUR)])


if __name__ == '__main__':
    asyncio.run(main())
