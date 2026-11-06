"""Paper trend bot logic: 1d Donchian 20/10, one paper position per coin.

Kept separate from main_trend.py (Telegram, exchange, loop) so every
decision can be tested with made-up candles.

Candles are ccxt lists: [timestamp ms, open, high, low, close, volume].
A daily candle that opened at 00:00 UTC is CLOSED once 24 hours have
passed; the one still running is the FORMING candle, and its open is the
price a decision made at the last close fills at.
"""

import csv
import os
from datetime import datetime, timezone

from grid_state import save_grid_state

DAY_MS = 86_400_000


def day_text(ms):
    """1730764800000 -> '2024-11-05'"""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')


def donchian_buy(closes, i, n):
    """Close i is above the highest of the previous n closes (not counting i)."""
    if i < n:
        return False
    return closes[i] > max(closes[i - n:i])


def donchian_sell(closes, i, m):
    """Close i is below the lowest of the previous m closes (not counting i)."""
    if i < m:
        return False
    return closes[i] < min(closes[i - m:i])


def paper_profit(entry_price, exit_price, buy_fee, sell_fee, usd):
    """Same math as the backtest: the buy fee comes out of the coins you get,
    the sell fee out of the proceeds."""
    coins = usd * (1 - buy_fee) / entry_price
    return coins * exit_price * (1 - sell_fee) - usd


def new_coin_state():
    return {'holding': False, 'entry_price': None, 'entry_time': None, 'last_checked': None}


def split_candles(candles, now_ms):
    """(closed candles oldest first, forming candle or None)."""
    candles = sorted(candles, key=lambda c: c[0])
    closed = [c for c in candles if c[0] + DAY_MS <= now_ms]
    forming = None
    if closed:
        for c in candles:
            if c[0] == closed[-1][0] + DAY_MS:
                forming = c
    return closed, forming


def process_coin(coin, state, candles, now_ms, entry_days, exit_days, fees, usd):
    """Check every closed candle not checked yet, oldest first.

    Returns (new state, events). Each buy or sell fills at the open of the
    candle after the signal: the forming candle for the newest close, or the
    next closed candle when catching up after the bot was off (those events
    are marked late=True, the fill is the price it WOULD have got).

    First run (nothing checked yet): only the newest close is checked, and
    the coin starts in cash. It doesn't replay history or jump into a trend
    that's already running; it waits for a fresh breakout.

    If the forming candle isn't there yet (Coinbase sometimes needs a moment
    after 00:00 UTC), the newest close is left unchecked for the next try.
    """
    state = dict(state)
    events = []
    closed, forming = split_candles(candles, now_ms)
    if not closed:
        return state, events
    closes = [c[4] for c in closed]

    if state['last_checked'] is None:
        to_check = [len(closed) - 1]
    else:
        to_check = [i for i, c in enumerate(closed) if c[0] > state['last_checked']]

    for i in to_check:
        fill = closed[i + 1] if i + 1 < len(closed) else forming
        if fill is None:
            break
        late = fill is not forming
        if not state['holding'] and donchian_buy(closes, i, entry_days):
            state.update(holding=True, entry_price=fill[1], entry_time=day_text(fill[0]))
            events.append({
                'coin': coin, 'action': 'buy', 'price': fill[1], 'time': day_text(fill[0]),
                'signal_close': closes[i], 'level': max(closes[i - entry_days:i]), 'late': late,
            })
        elif state['holding'] and donchian_sell(closes, i, exit_days):
            profit = paper_profit(state['entry_price'], fill[1], fees[0], fees[1], usd)
            days = (fill[0] - date_ms(state['entry_time'])) // DAY_MS
            events.append({
                'coin': coin, 'action': 'sell', 'price': fill[1], 'time': day_text(fill[0]),
                'signal_close': closes[i], 'level': min(closes[i - exit_days:i]), 'late': late,
                'entry_price': state['entry_price'], 'entry_time': state['entry_time'],
                'days': days, 'profit': profit,
            })
            state.update(holding=False, entry_price=None, entry_time=None)
        state['last_checked'] = closed[i][0]
    return state, events


def date_ms(day):
    """'2024-11-05' -> milliseconds at 00:00 UTC that day."""
    moment = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def event_message(event):
    """Telegram text for one paper buy or sell."""
    late = " (caught up: the bot was off when this happened)" if event['late'] else ""
    if event['action'] == 'buy':
        return (f"📈 PAPER BUY {event['coin']} at {event['price']} on {event['time']}{late}\n"
                f"Close {event['signal_close']} broke the 20-day high {event['level']}")
    sign = '+' if event['profit'] >= 0 else '-'
    return (f"📉 PAPER SELL {event['coin']} at {event['price']} on {event['time']}{late}\n"
            f"Close {event['signal_close']} broke the 10-day low {event['level']}\n"
            f"Bought {event['entry_time']} at {event['entry_price']}, held {event['days']} days: "
            f"{sign}${abs(event['profit']):.2f}")


TRADE_COLUMNS = ['coin', 'entry_time', 'entry_price', 'exit_time', 'exit_price', 'days', 'profit']


def append_trade(path, event):
    """Add one closed paper trade to the CSV (header written the first time)."""
    new_file = not os.path.exists(path)
    with open(path, 'a', newline='') as f:
        writer = csv.writer(f)
        if new_file:
            writer.writerow(TRADE_COLUMNS)
        writer.writerow([event['coin'], event['entry_time'], event['entry_price'], event['time'],
                         event['price'], event['days'], f"{event['profit']:.4f}"])


def read_profits(path):
    """Profits of every closed paper trade so far ([] if no file yet)."""
    if not os.path.exists(path):
        return []
    with open(path, newline='') as f:
        return [float(row['profit']) for row in csv.DictReader(f)]


def coin_line(coin, state, closes, entry_days, exit_days, fees, usd):
    """One summary line: what the coin is doing and how far it is from the
    next signal. `closes` are its closed daily closes, oldest first."""
    if len(closes) < max(entry_days, exit_days):
        return f"{coin}: not enough history yet"
    now = closes[-1]
    if state['holding']:
        level = min(closes[-exit_days:])
        open_profit = paper_profit(state['entry_price'], now, fees[0], fees[1], usd)
        sign = '+' if open_profit >= 0 else '-'
        return (f"{coin}: HOLDING since {state['entry_time']} at {state['entry_price']}, "
                f"now {now} ({sign}${abs(open_profit):.2f}). "
                f"Sells on a close below {level} ({(now - level) / now * 100:.1f}% away)")
    level = max(closes[-entry_days:])
    return (f"{coin}: cash. Buys on a close above {level} "
            f"(now {now}, {(level - now) / now * 100:.1f}% away)")


def summary_message(lines, profits):
    sign = '+' if sum(profits) >= 0 else '-'
    header = (f"📊 Trend paper (1d Donchian 20/10): {len(profits)} closed trades, "
              f"{sign}${abs(sum(profits)):.2f}")
    return "\n".join([header] + lines)


SUMMARY_KEY = '_summary_day'   # in the state file: the last day a summary went out


async def run_once(exchange, states, now_ms, alert, settings, log_error=print):
    """One check of every coin. Returns True if any coin checked a new day.

    exchange: ccxt exchange (only fetch_ohlcv is used: public prices)
    states:   {coin: state}, changed in place and saved to STATE_FILE
              (also holds SUMMARY_KEY, the last day summarized)
    alert:    async function that sends one Telegram message
    settings: trend_config (or anything with the same names)

    A daily candle only counts as closed SETTLE_MINUTES after it ends, so
    Coinbase has finished its closing price (right after 00:00 UTC the
    last close can still change).

    A coin whose prices can't be fetched is skipped this time (logged) and
    tried again next check; the others carry on.

    The summary goes out ONCE per day: the first check that processes a new
    day sends it. A coin that isn't ready yet (no first price of the new day
    to fill at) shows as waiting, and is processed on a later check; any
    buy or sell it makes then still gets its own alert.
    """
    import ccxt
    decide_ms = now_ms - settings.SETTLE_MINUTES * 60_000
    advanced = False
    newest_day = None
    lines = []
    for coin in settings.COINS:
        state = states.setdefault(coin, new_coin_state())
        try:
            candles = exchange.fetch_ohlcv(f"{coin}/USD", '1d', limit=settings.FETCH_DAYS)
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            log_error(f"{coin}: couldn't fetch candles: {e}")
            lines.append(f"{coin}: no prices this time ({type(e).__name__})")
            continue
        before = state['last_checked']
        new_state, events = process_coin(coin, state, candles, decide_ms, settings.ENTRY_DAYS,
                                         settings.EXIT_DAYS, settings.FEES,
                                         settings.USD_PER_TRADE)
        states[coin] = new_state
        if new_state['last_checked'] != before:
            advanced = True
            # save BEFORE logging, so a crash right here can't log a sale twice
            save_grid_state(states, settings.STATE_FILE)
        for event in events:
            if event['action'] == 'sell':
                append_trade(settings.TRADES_FILE, event)
            await alert(event_message(event))
        closed, _ = split_candles(candles, decide_ms)
        if not closed:
            lines.append(f"{coin}: no closed daily candles yet")
            continue
        newest_day = max(newest_day or 0, closed[-1][0])
        if new_state['last_checked'] != closed[-1][0]:
            lines.append(f"{coin}: waiting for the first price of the new day")
        else:
            lines.append(coin_line(coin, new_state, [c[4] for c in closed], settings.ENTRY_DAYS,
                                   settings.EXIT_DAYS, settings.FEES, settings.USD_PER_TRADE))
    if advanced and newest_day is not None and states.get(SUMMARY_KEY) != day_text(newest_day):
        states[SUMMARY_KEY] = day_text(newest_day)
        save_grid_state(states, settings.STATE_FILE)
        await alert(summary_message(lines, read_profits(settings.TRADES_FILE)))
    return advanced
