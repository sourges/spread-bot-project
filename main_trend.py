"""Trend PAPER bot: 1d Donchian 20/10 on the coins in trend_config.py.

Reads public Coinbase prices, places NO orders, needs no API keys.
Every CHECK_MINUTES it looks for a newly closed daily candle (they close
at 00:00 UTC = 7 PM EST / 8 PM EDT, and it waits SETTLE_MINUTES more so the
closing prices are final). Then it decides buy / sell / nothing per coin,
records paper trades, and sends Telegram alerts plus one summary a day.
Safe to restart: positions are saved in trend_paper_state.json.

Run: python main_trend.py   (or start_trend.bat). Ctrl+C to stop.
"""

import asyncio
import os
import time
from datetime import datetime

import ccxt
from dotenv import load_dotenv
from telegram import Bot
from telegram.request import HTTPXRequest

import trend_config
from grid_bot import write_error
from grid_state import load_grid_state
from telegram_send import send_with_retries
from trend_bot import new_coin_state, run_once

load_dotenv()
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')


def load_states(path, coins):
    """Saved positions, with a fresh (cash) state for any coin not saved yet."""
    states = load_grid_state(path) or {}
    for coin in coins:
        states.setdefault(coin, new_coin_state())
    return states


async def main():
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in .env")
        return

    telegram = Bot(token=TELEGRAM_TOKEN,
                   request=HTTPXRequest(connect_timeout=30, read_timeout=30,
                                        write_timeout=30, pool_timeout=30))

    def log_error(line):
        print(f"[ERROR] {line}")
        try:
            write_error(trend_config.ERRORS_FILE, 'trend paper', 'check', line)
        except OSError:
            pass

    async def send(message):
        await telegram.send_message(chat_id=CHAT_ID, text=message)

    async def alert(message):
        print(message)
        await send_with_retries(send, message, log=log_error)

    exchange = getattr(ccxt, trend_config.EXCHANGE)()   # public prices only
    states = load_states(trend_config.STATE_FILE, trend_config.COINS)
    holding = [c for c in trend_config.COINS if states[c]['holding']]
    print(f"🚀 Trend paper bot: {', '.join(trend_config.COINS)} "
          f"(holding: {', '.join(holding) or 'none'}). Ctrl+C to stop.")

    while True:
        now = datetime.now().strftime('%Y-%m-%d %H:%M')
        try:
            advanced = await run_once(exchange, states, int(time.time() * 1000), alert,
                                      trend_config, log_error)
            print(f"{now}  checked: {'new day processed' if advanced else 'no new daily close'}")
        except Exception as e:
            # anything unexpected: log it and try again next time instead of stopping
            log_error(f"unexpected: {type(e).__name__}: {e}")
        await asyncio.sleep(trend_config.CHECK_MINUTES * 60)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Stopped.")
