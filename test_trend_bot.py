import asyncio
import csv
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import ccxt

import main_trend
from trend_bot import (
    DAY_MS,
    SUMMARY_KEY,
    append_trade,
    coin_line,
    date_ms,
    day_text,
    donchian_buy,
    donchian_sell,
    event_message,
    new_coin_state,
    paper_profit,
    process_coin,
    read_profits,
    run_once,
    split_candles,
    summary_message,
)

START = date_ms('2026-01-01')
NO_FEES = (0, 0)


def day(i):
    return START + i * DAY_MS


def candles(pairs):
    """[(open, close), ...] -> ccxt daily candles starting 2026-01-01."""
    return [[day(i), o, o, o, c, 1] for i, (o, c) in enumerate(pairs)]


def during(i):
    """A moment halfway through day i (so day i is forming)."""
    return day(i) + DAY_MS // 2


# Donchian 3/2, same as test_trend in the mean-reversion repo:
#   d3 close 13 > highest of [10, 11, 12]  -> buy at d4 open 13.5
#   d4 close 14, lowest of [12, 13] is 12  -> hold
#   d5 close 12 < lowest of [13, 14]       -> sell at d6 open 11.5
#   d6 close 11.5: not above [13, 14, 12]  -> nothing
PAIRS = [(10, 10), (10, 11), (11, 12), (12, 13), (13.5, 14), (14, 12), (11.5, 11.5), (11, 11)]


def run(coin_state, upto, now_day, pairs=PAIRS):
    """process_coin with candles d0..d(upto) and the clock inside now_day."""
    return process_coin('CRV', coin_state, candles(pairs[:upto + 1]), during(now_day),
                        3, 2, NO_FEES, 100)


def checked(i, **changes):
    s = new_coin_state()
    s['last_checked'] = day(i)
    s.update(changes)
    return s


class TestDates(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(day_text(date_ms('2026-01-05')), '2026-01-05')

    def test_known_value(self):
        # 2026-01-01 00:00 UTC = 1,767,225,600 seconds
        self.assertEqual(date_ms('2026-01-01'), 1_767_225_600_000)


class TestSplit(unittest.TestCase):
    def test_closed_and_forming(self):
        closed, forming = split_candles(candles(PAIRS[:5]), during(4))
        self.assertEqual([c[0] for c in closed], [day(i) for i in range(4)])
        self.assertEqual(forming[0], day(4))

    def test_day_closes_exactly_at_midnight(self):
        # at 00:00 of d4, d3 has just closed and d4 has just started
        closed, forming = split_candles(candles(PAIRS[:5]), day(4))
        self.assertEqual(closed[-1][0], day(3))
        self.assertEqual(forming[0], day(4))

    def test_forming_not_sent_yet(self):
        closed, forming = split_candles(candles(PAIRS[:4]), during(4))
        self.assertEqual(closed[-1][0], day(3))
        self.assertIsNone(forming)

    def test_out_of_order(self):
        data = candles(PAIRS[:3])
        closed, _ = split_candles([data[2], data[0], data[1]], during(3))
        self.assertEqual([c[0] for c in closed], [day(0), day(1), day(2)])

    def test_empty(self):
        self.assertEqual(split_candles([], during(1)), ([], None))


class TestRule(unittest.TestCase):
    def test_buy(self):
        self.assertTrue(donchian_buy([1, 2, 3, 4], 3, 3))

    def test_buy_equal_is_not_above(self):
        self.assertFalse(donchian_buy([1, 3, 2, 3], 3, 3))

    def test_buy_oldest_counts(self):
        self.assertFalse(donchian_buy([5, 1, 2, 3], 3, 3))

    def test_buy_needs_history(self):
        self.assertFalse(donchian_buy([1, 2, 30], 2, 3))

    def test_sell(self):
        self.assertTrue(donchian_sell([5, 4, 6, 3], 3, 3))

    def test_sell_equal_is_not_below(self):
        self.assertFalse(donchian_sell([5, 4, 6, 4], 3, 3))

    def test_sell_oldest_counts(self):
        self.assertFalse(donchian_sell([1, 5, 6, 3], 3, 3))

    def test_sell_needs_history(self):
        self.assertFalse(donchian_sell([5, 1], 1, 3))


class TestProfit(unittest.TestCase):
    def test_no_fees(self):
        # $100 at 10 = 10 coins, sold at 12 = $120
        self.assertAlmostEqual(paper_profit(10, 12, 0, 0, 100), 20)

    def test_fees(self):
        # 99 / 10 = 9.9 coins; * 12 = 118.8; * 0.99 = 117.612
        self.assertAlmostEqual(paper_profit(10, 12, 0.01, 0.01, 100), 17.612)


class TestFirstRun(unittest.TestCase):
    def test_buy_signal_on_newest_close(self):
        state, events = run(new_coin_state(), upto=4, now_day=4)
        self.assertEqual(state, {'holding': True, 'entry_price': 13.5,
                                 'entry_time': '2026-01-05', 'last_checked': day(3)})
        self.assertEqual(events, [{'coin': 'CRV', 'action': 'buy', 'price': 13.5,
                                   'time': '2026-01-05', 'signal_close': 13, 'level': 12,
                                   'late': False}])

    def test_no_signal_starts_in_cash(self):
        state, events = run(new_coin_state(), upto=3, now_day=3)
        self.assertEqual(events, [])
        self.assertFalse(state['holding'])
        self.assertEqual(state['last_checked'], day(2))

    def test_does_not_replay_old_signals(self):
        # the d3 buy and d5 sell happened before the bot existed: only d6 is checked
        state, events = run(new_coin_state(), upto=7, now_day=7)
        self.assertEqual(events, [])
        self.assertFalse(state['holding'])
        self.assertEqual(state['last_checked'], day(6))

    def test_waits_for_the_forming_candle(self):
        # d3 says buy, but d4 (its fill) isn't there yet: check d3 again next time
        state, events = run(new_coin_state(), upto=3, now_day=4)
        self.assertEqual(events, [])
        self.assertIsNone(state['last_checked'])
        state, events = run(state, upto=4, now_day=4)
        self.assertEqual(events[0]['price'], 13.5)


class TestDayByDay(unittest.TestCase):
    def test_hold_then_sell(self):
        holding = checked(3, holding=True, entry_price=13.5, entry_time='2026-01-05')
        state, events = run(holding, upto=5, now_day=5)       # d4: hold
        self.assertEqual(events, [])
        self.assertTrue(state['holding'])
        self.assertEqual(state['last_checked'], day(4))
        state, events = run(state, upto=6, now_day=6)         # d5: sell at d6 open
        self.assertEqual(len(events), 1)
        sell = events[0]
        self.assertEqual((sell['action'], sell['price'], sell['time'], sell['late']),
                         ('sell', 11.5, '2026-01-07', False))
        self.assertEqual((sell['signal_close'], sell['level']), (12, 13))
        self.assertEqual((sell['entry_price'], sell['entry_time'], sell['days']),
                         (13.5, '2026-01-05', 2))
        # $100 at 13.5 = 7.4074 coins; * 11.5 = 85.1852 -> -14.8148
        self.assertAlmostEqual(sell['profit'], 100 / 13.5 * 11.5 - 100)
        self.assertEqual(state, checked(5))

    def test_nothing_new(self):
        state = checked(3)
        new_state, events = run(state, upto=4, now_day=4)
        self.assertEqual((new_state, events), (state, []))

    def test_does_not_change_the_input(self):
        state = checked(2)
        run(state, upto=4, now_day=4)
        self.assertEqual(state, checked(2))


class TestCatchUp(unittest.TestCase):
    def test_missed_sell_fills_at_the_next_open(self):
        # off from d4 until d7: d4 hold, d5 sell (fills at d6's open), d6 nothing
        holding = checked(3, holding=True, entry_price=13.5, entry_time='2026-01-05')
        state, events = run(holding, upto=7, now_day=7)
        self.assertEqual([(e['action'], e['price'], e['time'], e['late']) for e in events],
                         [('sell', 11.5, '2026-01-07', True)])
        self.assertEqual(state, checked(6))

    def test_catch_up_matches_the_backtest(self):
        # same trade the backtest makes on these candles: buy d4 13.5, sell d6 11.5
        state, events = run(checked(2), upto=7, now_day=7)
        self.assertEqual([(e['action'], e['time'], e['price'], e['late']) for e in events],
                         [('buy', '2026-01-05', 13.5, True), ('sell', '2026-01-07', 11.5, True)])

    def test_newest_fill_is_not_late(self):
        # off over d3 and d4, back during d5: d3 buy fills at d4 (late), d4 hold
        state, events = run(checked(2), upto=5, now_day=5)
        self.assertEqual([(e['action'], e['late']) for e in events], [('buy', True)])
        self.assertTrue(state['holding'])


class TestMessages(unittest.TestCase):
    def test_buy(self):
        event = {'coin': 'CRV', 'action': 'buy', 'price': 13.5, 'time': '2026-01-05',
                 'signal_close': 13, 'level': 12, 'late': False}
        self.assertEqual(event_message(event),
                         "📈 PAPER BUY CRV at 13.5 on 2026-01-05\n"
                         "Close 13 broke the 20-day high 12")

    def test_sell_loss_late(self):
        event = {'coin': 'CRV', 'action': 'sell', 'price': 11.5, 'time': '2026-01-07',
                 'signal_close': 12, 'level': 13, 'late': True, 'entry_price': 13.5,
                 'entry_time': '2026-01-05', 'days': 2, 'profit': -14.8148}
        self.assertEqual(event_message(event),
                         "📉 PAPER SELL CRV at 11.5 on 2026-01-07 "
                         "(caught up: the bot was off when this happened)\n"
                         "Close 12 broke the 10-day low 13\n"
                         "Bought 2026-01-05 at 13.5, held 2 days: -$14.81")

    def test_sell_win(self):
        event = {'coin': 'BTC', 'action': 'sell', 'price': 12, 'time': '2026-02-01',
                 'signal_close': 12.1, 'level': 12.2, 'late': False, 'entry_price': 10,
                 'entry_time': '2026-01-01', 'days': 31, 'profit': 20}
        self.assertTrue(event_message(event).endswith("held 31 days: +$20.00"))


class TestTradesFile(unittest.TestCase):
    def sell(self, coin, profit):
        return {'coin': coin, 'entry_time': '2026-01-05', 'entry_price': 13.5,
                'time': '2026-01-07', 'price': 11.5, 'days': 2, 'profit': profit}

    def test_append_and_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 't.csv')
            append_trade(path, self.sell('CRV', -14.81481))
            append_trade(path, self.sell('BTC', 5))
            with open(path, newline='') as f:
                rows = list(csv.reader(f))
            profits = read_profits(path)
        self.assertEqual(rows[0], ['coin', 'entry_time', 'entry_price', 'exit_time',
                                   'exit_price', 'days', 'profit'])
        self.assertEqual(rows[1], ['CRV', '2026-01-05', '13.5', '2026-01-07', '11.5', '2',
                                   '-14.8148'])
        self.assertEqual(len(rows), 3)
        self.assertEqual(profits, [-14.8148, 5.0])

    def test_no_file_yet(self):
        self.assertEqual(read_profits('does_not_exist.csv'), [])


class TestSummary(unittest.TestCase):
    def test_cash_line(self):
        # buys above the highest of the last 3 closes (12); 12 is 20% above 10
        self.assertEqual(coin_line('UNI', new_coin_state(), [10, 12, 11, 10], 3, 2, NO_FEES, 100),
                         "UNI: cash. Buys on a close above 12 (now 10, 20.0% away)")

    def test_holding_line(self):
        # sells below the lowest of the last 2 closes (10); open: $100 at 8 -> 10 = +25
        state = {'holding': True, 'entry_price': 8, 'entry_time': '2026-01-02',
                 'last_checked': None}
        self.assertEqual(coin_line('ETH', state, [12, 13, 11, 12.5], 3, 2, NO_FEES, 100),
                         "ETH: HOLDING since 2026-01-02 at 8, now 12.5 (+$56.25). "
                         "Sells on a close below 11 (12.0% away)")

    def test_not_enough_history(self):
        self.assertEqual(coin_line('CAKE', new_coin_state(), [1, 2], 3, 2, NO_FEES, 100),
                         "CAKE: not enough history yet")

    def test_summary_message(self):
        self.assertEqual(summary_message(["a", "b"], [10, -2.5]),
                         "📊 Trend paper (1d Donchian 20/10): 2 closed trades, +$7.50\na\nb")

    def test_summary_loss(self):
        self.assertTrue(summary_message([], [-3]).endswith("1 closed trades, -$3.00"))


class FakeExchange:
    def __init__(self, data, errors=None):
        self.data = data
        self.errors = errors or {}
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls.append((symbol, timeframe, limit))
        coin = symbol.split('/')[0]
        if coin in self.errors:
            raise self.errors[coin]
        return self.data[coin]


class RunOnceSetup:
    """Shared setup for the run_once tests (not a test class itself)."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.settings = SimpleNamespace(
            COINS=['AAA', 'BBB'], ENTRY_DAYS=3, EXIT_DAYS=2, FEES=NO_FEES, USD_PER_TRADE=100,
            FETCH_DAYS=60, SETTLE_MINUTES=60,
            STATE_FILE=os.path.join(self.folder.name, 's.json'),
            TRADES_FILE=os.path.join(self.folder.name, 't.csv'))
        self.sent = []
        self.errors = []

    def tearDown(self):
        self.folder.cleanup()

    async def alert(self, message):
        self.sent.append(message)

    def check(self, exchange, states, now):
        return asyncio.run(run_once(exchange, states, now, self.alert, self.settings,
                                    self.errors.append))

    def flat(self, n):
        return candles([(5, 5)] * n)


class TestRunOnce(RunOnceSetup, unittest.TestCase):

    def test_first_check(self):
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        states = {}
        self.assertTrue(self.check(exchange, states, during(4)))
        self.assertEqual(exchange.calls, [('AAA/USD', '1d', 60), ('BBB/USD', '1d', 60)])
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[0].startswith("📈 PAPER BUY AAA at 13.5"))
        self.assertEqual(self.sent[1].splitlines(), [
            "📊 Trend paper (1d Donchian 20/10): 0 closed trades, +$0.00",
            # AAA: $100 at 13.5, now 13: -3.70. Lowest of the last 2 closes is 12
            "AAA: HOLDING since 2026-01-05 at 13.5, now 13 (-$3.70). "
            "Sells on a close below 12 (7.7% away)",
            "BBB: cash. Buys on a close above 5 (now 5, 0.0% away)",
        ])
        with open(self.settings.STATE_FILE) as f:
            saved = json.load(f)
        self.assertTrue(saved['AAA']['holding'])
        self.assertFalse(saved['BBB']['holding'])

    def test_same_day_again_sends_nothing(self):
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        states = {}
        self.check(exchange, states, during(4))
        self.sent.clear()
        self.assertFalse(self.check(exchange, states, during(4) + 15 * 60_000))
        self.assertEqual(self.sent, [])

    def test_sell_is_logged_and_counted(self):
        states = {'AAA': checked(4, holding=True, entry_price=13.5, entry_time='2026-01-05'),
                  'BBB': checked(5)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:7]), 'BBB': self.flat(7)})
        self.check(exchange, states, during(6))
        self.assertTrue(self.sent[0].startswith("📉 PAPER SELL AAA at 11.5"))
        self.assertTrue(self.sent[-1].startswith(
            "📊 Trend paper (1d Donchian 20/10): 1 closed trades, -$14.81"))
        self.assertEqual(read_profits(self.settings.TRADES_FILE), [-14.8148])

    def test_state_saved_before_the_sell_is_logged(self):
        states = {'AAA': checked(4, holding=True, entry_price=13.5, entry_time='2026-01-05'),
                  'BBB': checked(5)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:7]), 'BBB': self.flat(7)})
        seen = []

        async def alert(message):
            with open(self.settings.STATE_FILE) as f:
                seen.append(json.load(f)['AAA']['holding'])

        asyncio.run(run_once(exchange, states, during(6), alert, self.settings))
        self.assertEqual(seen[0], False)   # already saved as sold when the alert went out

    def test_fetch_error_skips_only_that_coin(self):
        exchange = FakeExchange({'AAA': candles(PAIRS[:5])},
                                errors={'BBB': ccxt.NetworkError('timeout')})
        states = {}
        self.assertTrue(self.check(exchange, states, during(4)))
        self.assertEqual(len(self.errors), 1)
        self.assertIn("BBB: couldn't fetch candles", self.errors[0])
        self.assertIn("BBB: no prices this time (NetworkError)", self.sent[-1])
        self.assertTrue(states['AAA']['holding'])
        self.assertIsNone(states['BBB']['last_checked'])

    def test_exchange_error_also_skipped(self):
        exchange = FakeExchange({'AAA': candles(PAIRS[:5])},
                                errors={'BBB': ccxt.BadSymbol('no such market')})
        self.check(exchange, {}, during(4))
        self.assertIn("(BadSymbol)", self.sent[-1])

    def test_nothing_new_no_summary(self):
        exchange = FakeExchange({'AAA': self.flat(4), 'BBB': self.flat(4)})
        states = {'AAA': checked(2), 'BBB': checked(2)}
        self.assertFalse(self.check(exchange, states, during(3)))
        self.assertEqual(self.sent, [])
        self.assertFalse(os.path.exists(self.settings.STATE_FILE))


class TestSettleAndOneSummary(RunOnceSetup, unittest.TestCase):
    MINUTE = 60_000

    def test_waits_an_hour_after_the_close(self):
        # d3 closes at 00:00 of d4. At 00:30 it isn't final yet: nothing happens
        states = {'AAA': checked(2), 'BBB': checked(2)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.assertFalse(self.check(exchange, states, day(4) + 30 * self.MINUTE))
        self.assertEqual(self.sent, [])
        self.assertEqual(states['AAA']['last_checked'], day(2))
        # at 01:01 it is: AAA's d3 breakout buys at d4's open
        self.assertTrue(self.check(exchange, states, day(4) + 61 * self.MINUTE))
        self.assertTrue(self.sent[0].startswith("📈 PAPER BUY AAA at 13.5"))

    def test_exactly_one_hour_counts(self):
        states = {'AAA': checked(2), 'BBB': checked(2)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.assertTrue(self.check(exchange, states, day(4) + 60 * self.MINUTE))

    def test_late_coin_waits_and_only_one_summary(self):
        # BBB has no d4 price yet: AAA buys, the summary shows BBB waiting
        states = {}
        late = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': candles(PAIRS[:4])})
        self.assertTrue(self.check(late, states, during(4)))
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[0].startswith("📈 PAPER BUY AAA"))
        self.assertIn("BBB: waiting for the first price of the new day", self.sent[1])
        self.assertIsNone(states['BBB']['last_checked'])
        # 15 minutes later BBB's d4 price is there: BBB buys, NO second summary
        self.sent.clear()
        ready = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': candles(PAIRS[:5])})
        self.assertTrue(self.check(ready, states, during(4) + 15 * self.MINUTE))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("📈 PAPER BUY BBB"))

    def test_next_day_gets_its_own_summary(self):
        states = {}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.check(exchange, states, during(4))
        self.sent.clear()
        exchange = FakeExchange({'AAA': candles(PAIRS[:6]), 'BBB': self.flat(6)})
        self.check(exchange, states, during(5))
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(self.sent[0].startswith("📊 Trend paper"))

    def test_summary_day_is_saved(self):
        # the summary for the d3 close (2026-01-04) is remembered across restarts
        states = {}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.check(exchange, states, during(4))
        with open(self.settings.STATE_FILE) as f:
            self.assertEqual(json.load(f)[SUMMARY_KEY], '2026-01-04')

    def test_restart_does_not_resend(self):
        # restarted after the summary went out, with BBB still to process
        states = {SUMMARY_KEY: '2026-01-04', 'AAA': checked(3), 'BBB': checked(2)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.assertTrue(self.check(exchange, states, during(4)))
        self.assertEqual(self.sent, [])

    def test_catch_up_inside_the_wait_keeps_the_real_summary(self):
        # restarted at 00:30 of d4 after being off: d2 is caught up now (its
        # summary is for 2026-01-03); d3 isn't final until 01:00 and must still
        # get its own summary then
        states = {'AAA': checked(1), 'BBB': checked(1)}
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': self.flat(5)})
        self.check(exchange, states, day(4) + 30 * self.MINUTE)
        self.assertEqual(states[SUMMARY_KEY], '2026-01-03')
        self.check(exchange, states, day(4) + 61 * self.MINUTE)
        summaries = [m for m in self.sent if m.startswith("📊")]
        self.assertEqual(len(summaries), 2)
        self.assertEqual(states[SUMMARY_KEY], '2026-01-04')

    def test_summary_day_is_the_newest_of_all_coins(self):
        # AAA's data stops at d2 (stale), BBB is up to date: the day is d3's
        exchange = FakeExchange({'AAA': candles(PAIRS[:3]), 'BBB': self.flat(5)})
        states = {}
        self.check(exchange, states, during(4))
        self.assertEqual(states[SUMMARY_KEY], '2026-01-04')

    def test_coin_with_no_closed_candles(self):
        # BBB's only candle is today's (d4), still forming: nothing closed yet
        exchange = FakeExchange({'AAA': candles(PAIRS[:5]), 'BBB': [[day(4), 5, 5, 5, 5, 1]]})
        self.check(exchange, {}, during(4))
        self.assertIn("BBB: no closed daily candles yet", self.sent[-1])


class TestLoadStates(unittest.TestCase):
    def test_no_file(self):
        states = main_trend.load_states('does_not_exist.json', ['CRV', 'BTC'])
        self.assertEqual(states, {'CRV': new_coin_state(), 'BTC': new_coin_state()})

    def test_keeps_saved_and_adds_new_coins(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 's.json')
            saved = {'CRV': checked(3, holding=True, entry_price=0.4, entry_time='2026-01-04')}
            with open(path, 'w') as f:
                json.dump(saved, f)
            states = main_trend.load_states(path, ['CRV', 'BTC'])
        self.assertEqual(states['CRV'], saved['CRV'])
        self.assertEqual(states['BTC'], new_coin_state())

    def test_keeps_the_summary_day(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, 's.json')
            with open(path, 'w') as f:
                json.dump({SUMMARY_KEY: '2026-11-05'}, f)
            states = main_trend.load_states(path, ['CRV'])
        self.assertEqual(states[SUMMARY_KEY], '2026-11-05')


class TestMainStartup(unittest.TestCase):
    def test_stops_without_telegram_settings(self):
        import contextlib, io
        from unittest import mock
        out = io.StringIO()
        with mock.patch.object(main_trend, 'TELEGRAM_TOKEN', None), \
             contextlib.redirect_stdout(out):
            asyncio.run(main_trend.main())
        self.assertIn("Missing TELEGRAM_BOT_TOKEN", out.getvalue())


class TestConfig(unittest.TestCase):
    def test_settings_match_the_tested_rule(self):
        import trend_config
        self.assertEqual(trend_config.COINS, ['CRV', 'BTC', 'ETH', 'UNI', 'DOGE', 'CAKE'])
        self.assertEqual((trend_config.ENTRY_DAYS, trend_config.EXIT_DAYS), (20, 10))
        self.assertEqual(trend_config.FEES, (0.0038, 0.0038))
        self.assertEqual(trend_config.EXCHANGE, 'coinbase')
        self.assertEqual(trend_config.SETTLE_MINUTES, 60)
        # enough candles for the 20-day rule plus a few weeks of catching up
        self.assertGreaterEqual(trend_config.FETCH_DAYS, trend_config.ENTRY_DAYS + 30)


if __name__ == '__main__':
    unittest.main()
