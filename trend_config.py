# Trend paper bot settings (main_trend.py). Change here on the main PC,
# push, then git pull on the 24/7 PC and restart the bot.
#
# PAPER ONLY: it reads public prices and places NO orders. No API keys.
#
# Rule (1d Donchian 20/10, tested in the mean-reversion repo):
#   buy  when a daily close is above the highest of the previous ENTRY_DAYS closes
#   sell when a daily close is below the lowest  of the previous EXIT_DAYS closes
#   fills at the next day's open, like the backtest.

EXCHANGE = 'coinbase'   # where the prices come from (same as the backtest)
COINS = ['CRV', 'BTC', 'ETH', 'UNI', 'DOGE', 'CAKE']   # traded against USD
ENTRY_DAYS = 20
EXIT_DAYS = 10
USD_PER_TRADE = 100     # paper dollars per buy, per coin
FEES = (0.0038, 0.0038) # Kraken taker, buy and sell (same as the backtest)

CHECK_MINUTES = 15      # how often it looks for a newly closed daily candle
SETTLE_MINUTES = 60     # wait this long after the daily close (00:00 UTC) before
                        # deciding, so Coinbase has finished the closing price:
                        # decisions and the summary come at ~8 PM EST / 9 PM EDT
FETCH_DAYS = 60         # daily candles fetched per check (enough to catch up
                        # after being off for a few weeks)

STATE_FILE = 'trend_paper_state.json'
TRADES_FILE = 'trend_paper_trades.csv'   # has a 'profit' column for report.py
ERRORS_FILE = 'trend_errors.txt'
