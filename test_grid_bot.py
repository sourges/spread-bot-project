import asyncio
import contextlib
import csv
import io
import os
import unittest
import ccxt
from datetime import datetime
from grid_bot import (GridBot, OrderProblem, run_bots, status_lines, seconds_until,
                      summary_text, send_summary, funding_report, check_funding)
from grid_state import load_grid_state, save_grid_state


class FakeExchange:
    """Pretends to be Kraken. Orders live in a dict; tests fill them by hand."""

    id = 'kraken'

    def __init__(self, price):
        self.price = price
        self.orders = {}
        self.next_id = 1
        self.fill_on_cancel = set()     # ids that "fill just before the cancel arrives"
        self.lose_next_reply = False    # next order goes through, but we get an error back
        self.no_funds_for = set()       # 'buy'/'sell': entries on that side fail for lack of funds
        self.balance = {}               # what fetch_balance() reports as 'total'
        self.bid = None                 # set both to test the bid/ask mid-price
        self.ask = None

    def create_order(self, symbol, type, side, amount, price, params=None):
        if side in self.no_funds_for and (params or {}).get('postOnly'):
            raise ccxt.InsufficientFunds("EOrder:Insufficient funds")
        order_id = f"O{self.next_id}"
        self.next_id += 1
        self.orders[order_id] = {
            'id': order_id, 'side': side, 'amount': amount, 'price': price,
            'status': 'open', 'filled': 0.0, 'cost': 0.0, 'average': None,
            'fee': None, 'params': params or {},
            'clientOrderId': (params or {}).get('clientOrderId'),
        }
        if self.lose_next_reply:
            self.lose_next_reply = False
            raise ccxt.NetworkError("timed out (but the order went through)")
        return dict(self.orders[order_id])

    def fetch_order(self, order_id, symbol):
        if order_id not in self.orders:
            raise ccxt.OrderNotFound(order_id)
        return dict(self.orders[order_id])

    def cancel_order(self, order_id, symbol):
        if order_id in self.fill_on_cancel:
            self.fill(order_id)
        order = self.orders[order_id]
        if order['status'] != 'open':
            raise ccxt.InvalidOrder(f"{order_id} is not open")
        order['status'] = 'canceled'

    def fetch_ticker(self, symbol):
        return {'last': self.price, 'bid': self.bid, 'ask': self.ask}

    def fetch_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o['status'] == 'open']

    def fetch_balance(self):
        return {'total': dict(self.balance)}

    def fetch_closed_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o['status'] != 'open']

    # ----- test helpers -----
    def fill(self, order_id, amount=None):
        """Fill an order (fully by default). Fee is 0.3% in USD, like Kraken maker."""
        order = self.orders[order_id]
        filled = order['amount'] if amount is None else amount
        order['filled'] = filled
        order['cost'] = filled * order['price']
        order['average'] = order['price']
        order['fee'] = {'cost': order['cost'] * 0.003, 'currency': 'USD'}
        if filled == order['amount']:
            order['status'] = 'closed'

    def open_at(self, side, price):
        """The open order on this side at this price, or None."""
        for o in self.orders.values():
            if o['status'] == 'open' and o['side'] == side and o['price'] == price:
                return o
        return None


def trade_tag(bot, entry):
    """The exit tag saved on the trade for this entry order."""
    return [t for t in bot.trades if t['entry_id'] == entry['id']][0]['exit_tag']


class GridBotTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        self.state_file = 'test_grid_state.json'
        self.log_file = 'test_grid_trades.txt'
        self.csv_file = 'test_grid_completed.csv'
        self.errors_file = 'test_grid_errors.txt'      # never the real grid_errors.txt
        self.exchange = FakeExchange(price=0.3795)
        self.alerts = []
        self.bot = self.make_bot(self.levels)

    def make_bot(self, levels):
        async def alert(message):
            self.alerts.append(message)
        return GridBot(self.exchange, 'CRV/USD', levels, 61, 2, 20, alert,
                       count=2, check_interval=0,
                       state_file=self.state_file, log_file=self.log_file,
                       csv_file=self.csv_file, errors_file=self.errors_file)

    def tearDown(self):
        for f in (self.state_file, self.log_file, self.csv_file, self.errors_file):
            if os.path.exists(f):
                os.remove(f)

    async def test_first_cycle_places_entries(self):
        await self.bot.run_cycle()
        # longs at the 2 levels below 0.3795, short at the only level above
        buy1 = self.exchange.open_at('buy', 0.36964)
        buy2 = self.exchange.open_at('buy', 0.35884)
        sell = self.exchange.open_at('sell', 0.38077)
        self.assertEqual(buy1['amount'], 165.02)     # 61 / 0.36964 = 165.025... rounded down
        self.assertEqual(buy2['amount'], 169.99)     # 61 / 0.35884 = 169.992...
        self.assertEqual(sell['amount'], 160.2)      # 61 / 0.38077 = 160.201...
        self.assertEqual(buy1['params'], {'postOnly': True})
        self.assertEqual(len(self.exchange.fetch_open_orders('CRV/USD')), 3)
        self.assertEqual(len(load_grid_state(self.state_file)['trades']), 3)

    async def test_second_cycle_changes_nothing(self):
        await self.bot.run_cycle()
        await self.bot.run_cycle()
        self.assertEqual(len(self.exchange.orders), 3)       # no extra orders created

    async def test_entry_fill_places_exit(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()

        exit_sell = self.exchange.open_at('sell', 0.38077)
        self.assertEqual(exit_sell['amount'], 165.02)        # sells exactly what was bought
        # exits are not post-only, but carry the bot's tag
        self.assertEqual(exit_sell['params'], {'clientOrderId': trade_tag(self.bot, buy)})
        trade = [t for t in self.bot.trades if t['entry_id'] == buy['id']][0]
        self.assertEqual(trade['status'], 'exit_open')
        self.assertEqual(trade['exit_id'], exit_sell['id'])
        # 0.36964 and 0.38077 are now taken: new long at 0.34835,
        # and the short entry at 0.38077 is cancelled
        self.assertIsNotNone(self.exchange.open_at('buy', 0.34835))
        self.assertEqual(self.exchange.open_at('sell', 0.38077)['id'], exit_sell['id'])

    async def test_exit_fill_records_profit(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        exit_sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(exit_sell['id'])
        await self.bot.run_cycle()

        # by hand: price gain minus 0.3% fee on both sides
        expected = (0.38077 - 0.36964) * 165.02 - 0.003 * 165.02 * (0.36964 + 0.38077)
        self.assertAlmostEqual(self.bot.total_profit, expected, places=6)
        self.assertEqual(self.bot.completed, 1)
        ids = [t['entry_id'] for t in self.bot.trades]
        self.assertNotIn(buy['id'], ids)

    async def test_short_trade_profit(self):
        await self.bot.run_cycle()
        sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(sell['id'])
        await self.bot.run_cycle()
        exit_buy = self.exchange.open_at('buy', 0.36964)
        self.assertEqual(exit_buy['amount'], 160.2)          # buys back the same amount
        self.exchange.fill(exit_buy['id'])
        await self.bot.run_cycle()

        expected = (0.38077 - 0.36964) * 160.2 - 0.003 * 160.2 * (0.38077 + 0.36964)
        self.assertAlmostEqual(self.bot.total_profit, expected, places=6)

    async def test_price_drop_cancels_far_entry(self):
        await self.bot.run_cycle()
        old_buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.price = 0.355          # targets become 0.34835 and 0.33818
        await self.bot.run_cycle()

        self.assertEqual(self.exchange.orders[old_buy['id']]['status'], 'canceled')
        self.assertIsNotNone(self.exchange.open_at('buy', 0.34835))
        self.assertIsNotNone(self.exchange.open_at('buy', 0.33818))

    async def test_cancel_race_becomes_exit(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill_on_cancel.add(buy['id'])   # fills the instant we try to cancel
        self.exchange.price = 0.355
        await self.bot.run_cycle()

        trade = [t for t in self.bot.trades if t['entry_id'] == buy['id']][0]
        self.assertEqual(trade['status'], 'exit_open')
        self.assertIsNotNone(self.exchange.open_at('sell', 0.38077))

    async def test_tiny_partial_fill_is_dropped(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'], amount=5)      # 5 CRV, under the 20 minimum
        self.exchange.price = 0.355
        await self.bot.run_cycle()

        ids = [t['entry_id'] for t in self.bot.trades]
        self.assertNotIn(buy['id'], ids)
        self.assertTrue(any('below the minimum' in a for a in self.alerts))

    async def test_restart_recovers_trades_and_levels(self):
        await self.bot.run_cycle()
        saved_trades = list(self.bot.trades)

        new_bot = self.make_bot([0.1, 0.2, 0.3])     # different settings on purpose
        await new_bot.recover()
        await new_bot.run_cycle()

        self.assertEqual(new_bot.levels, self.levels)   # saved levels win
        self.assertEqual(new_bot.trades, saved_trades)
        self.assertEqual(len(self.exchange.orders), 3)  # nothing new placed

    async def test_unknown_order_is_reported_not_cancelled(self):
        stray = self.exchange.create_order('CRV/USD', 'limit', 'buy', 100, 0.30)
        await self.bot.recover()
        await self.bot.run_cycle()

        self.assertTrue(any(stray['id'] in a for a in self.alerts))
        self.assertEqual(self.exchange.orders[stray['id']]['status'], 'open')

    async def test_alerts_say_which_bot(self):
        await self.bot.alert('hello')
        self.assertEqual(self.alerts, ['[kraken CRV/USD] hello'])

    async def test_terminal_lines_say_which_bot(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):        # catch what print() writes
            await self.bot.run_cycle()
        lines = out.getvalue().splitlines()
        self.assertTrue(lines)
        self.assertTrue(all(line.startswith('[kraken CRV/USD] ') for line in lines))

    def test_default_file_names_include_exchange_and_pair(self):
        async def alert(message):
            pass
        bot = GridBot(self.exchange, 'CRV/USD', self.levels, 61, 2, 20, alert,
                      errors_file=self.errors_file)
        self.assertEqual(bot.state_file, 'grid_state_kraken_CRV_USD.json')
        self.assertEqual(bot.log_file, 'grid_trades_kraken_CRV_USD.txt')

    async def test_state_file_from_another_pair_stops(self):
        save_grid_state({'exchange': 'kraken', 'pair': 'CAKE/USD', 'levels': self.levels,
                         'trades': [], 'total_profit': 0.0, 'completed': 0}, self.state_file)
        # timeout: if the check were missing, the bot would trade forever
        await asyncio.wait_for(self.bot.run_continuous(), timeout=2)

        self.assertTrue(any('🛑' in a and 'CAKE/USD' in a for a in self.alerts))
        self.assertEqual(self.exchange.orders, {})      # nothing placed

    async def test_old_state_without_pair_still_recovers(self):
        await self.bot.run_cycle()
        state = load_grid_state(self.state_file)
        del state['pair'], state['exchange']            # like a file saved before v2
        save_grid_state(state, self.state_file)

        new_bot = self.make_bot(self.levels)
        await new_bot.recover()
        self.assertEqual(len(new_bot.trades), 3)

    async def test_one_bot_crashing_does_not_stop_the_other(self):
        class BrokenExchange(FakeExchange):
            def fetch_ticker(self, symbol):
                raise RuntimeError("boom")

        async def alert(message):
            self.alerts.append(message)

        broken = GridBot(BrokenExchange(price=0.3795), 'CAKE/USD', self.levels, 61, 2, 20,
                         alert, count=2, check_interval=0,
                         state_file='test_state_cake.json', log_file='test_log_cake.txt',
                         errors_file=self.errors_file)
        try:
            # run_bots never ends on its own while one bot is healthy, so stop it after 0.5s
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(run_bots([broken, self.bot]), timeout=0.5)
        finally:
            for f in ('test_state_cake.json', 'test_log_cake.txt'):
                if os.path.exists(f):
                    os.remove(f)

        self.assertTrue(any('[kraken CAKE/USD] 🛑' in a for a in self.alerts))
        self.assertEqual(len(self.exchange.fetch_open_orders('CRV/USD')), 3)   # CRV bot kept trading
        self.assertTrue(any('[kraken CRV/USD] ⏹️' in a for a in self.alerts))   # and stopped cleanly

    async def test_slow_cancel_is_not_reported_as_outside(self):
        """Coinbase: right after a cancel, the order can still show 'open'."""
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.35884)            # the far long entry
        real_cancel = self.exchange.cancel_order
        self.exchange.cancel_order = lambda order_id, symbol: None   # cancel "not done yet"

        self.exchange.price = 0.33                              # far entry must be cancelled
        await self.bot.run_cycle()
        self.exchange.orders[buy['id']]['status'] = 'canceled'  # cancel finishes later
        self.exchange.cancel_order = real_cancel
        await self.bot.run_cycle()

        self.assertFalse(any('outside the bot' in a for a in self.alerts))
        self.assertNotIn(buy['id'], [t['entry_id'] for t in self.bot.trades])

    async def test_exit_cancelled_outside_bot_stops(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        exit_sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.orders[exit_sell['id']]['status'] = 'canceled'

        with self.assertRaises(OrderProblem):
            await self.bot.run_cycle()


    async def test_numbers_as_text_still_work(self):
        # Kraken/ccxt sometimes returns cost/fee/filled as strings (the Oct 15 crash)
        await self.bot.run_cycle()
        sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(sell['id'])
        await self.bot.run_cycle()
        exit_buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(exit_buy['id'])
        o = self.exchange.orders[exit_buy['id']]
        o['cost'] = str(o['cost'])
        o['filled'] = str(o['filled'])
        o['fee'] = {'cost': str(o['fee']['cost']), 'currency': 'USD'}
        await self.bot.run_cycle()

        expected = (0.38077 - 0.36964) * 160.2 - 0.003 * 160.2 * (0.38077 + 0.36964)
        self.assertAlmostEqual(self.bot.total_profit, expected, places=6)
        self.assertEqual(self.bot.completed, 1)


    async def test_old_saved_text_numbers_still_work(self):
        # a trade saved before the fix, with entry_cost/entry_fee stored as text
        await self.bot.run_cycle()
        sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(sell['id'])
        await self.bot.run_cycle()
        trade = [t for t in self.bot.trades if t['entry_id'] == sell['id']][0]
        trade['entry_cost'] = str(trade['entry_cost'])
        trade['entry_fee'] = str(trade['entry_fee'])
        exit_buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(exit_buy['id'])
        await self.bot.run_cycle()

        expected = (0.38077 - 0.36964) * 160.2 - 0.003 * 160.2 * (0.38077 + 0.36964)
        self.assertAlmostEqual(self.bot.total_profit, expected, places=6)
        self.assertEqual(self.bot.completed, 1)


    # ---------- V2 #6: duplicate exits ----------

    def orders_with_tag(self, tag):
        return [o for o in self.exchange.orders.values() if o['clientOrderId'] == tag]

    async def test_lost_reply_does_not_place_second_exit(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        self.exchange.lose_next_reply = True        # exit reaches the exchange, reply is lost
        await self.bot.run_cycle()

        trade = [t for t in self.bot.trades if t['entry_id'] == buy['id']][0]
        tagged = self.orders_with_tag(trade['exit_tag'])
        self.assertEqual(len(tagged), 1)                    # one exit, not two
        self.assertEqual(trade['exit_id'], tagged[0]['id'])
        self.assertEqual(trade['status'], 'exit_open')

    async def test_restart_after_crash_finds_existing_exit(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        # Crash: tag saved, exit placed, but the bot died before saving exit_id.
        trade = [t for t in self.bot.trades if t['entry_id'] == buy['id']][0]
        trade['exit_tag'] = 'tag-1'
        self.bot.save()
        placed = self.exchange.create_order('CRV/USD', 'limit', 'sell', 165.02, 0.38077,
                                            {'clientOrderId': 'tag-1'})

        new_bot = self.make_bot(self.levels)
        await new_bot.recover()
        await new_bot.run_cycle()

        self.assertEqual(len(self.orders_with_tag('tag-1')), 1)    # not placed again
        trade = [t for t in new_bot.trades if t['entry_id'] == buy['id']][0]
        self.assertEqual(trade['exit_id'], placed['id'])
        self.assertEqual(trade['status'], 'exit_open')

    def test_tag_params_per_exchange(self):
        self.assertEqual(self.bot.tag_params('abc'), {'clientOrderId': 'abc'})
        self.exchange.id = 'coinbase'
        self.assertEqual(self.bot.tag_params('abc'), {'client_order_id': 'abc'})


    # ---------- screen block and daily summary ----------

    async def do_one_long_trade(self):
        """Buy at 0.36964 fills, then its exit at 0.38077 fills. 3 cycles."""
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        exit_sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(exit_sell['id'])
        await self.bot.run_cycle()

    def test_seconds_until(self):
        self.assertEqual(seconds_until(9, datetime(2026, 10, 22, 8, 0)), 3600)      # 1 hour
        self.assertEqual(seconds_until(9, datetime(2026, 10, 22, 9, 0)), 86400)     # exactly 9:00 -> tomorrow
        self.assertEqual(seconds_until(9, datetime(2026, 10, 22, 10, 30)), 81000)   # 22.5 hours

    async def test_status_line_after_first_cycle(self):
        self.assertEqual(status_lines([self.bot], 0), ["[kraken CRV/USD] starting..."])
        await self.bot.run_cycle()
        now = self.bot.last_cycle
        # first cycle: 2 longs + 1 short placed, nothing done yet
        self.assertEqual(status_lines([self.bot], now),
                         ["[kraken CRV/USD] Price 0.3795 | open trades: 3 | done: 0 | profit $0.0000"])

    async def test_status_line_warns_when_no_cycle_for_10_min(self):
        await self.bot.run_cycle()
        # A round number, not time.monotonic(): with a float like 261891.47608289847,
        # (x + 600) - x comes out as 599.99999999997, so "10 min" became 9 (flaky test).
        self.bot.last_cycle = 1000.0
        line = status_lines([self.bot], self.bot.last_cycle + 599)[0]       # 9 min 59 s: fine
        self.assertNotIn('⚠️', line)
        line = status_lines([self.bot], self.bot.last_cycle + 600)[0]       # 10 min
        self.assertTrue(line.endswith("  ⚠️ no cycle for 10 min"))

    async def test_stopped_bot_shows_stopped(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        exit_sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.orders[exit_sell['id']]['status'] = 'canceled'

        await self.bot.run_continuous()         # hits the OrderProblem, alerts, returns
        self.assertEqual(self.bot.status, 'stopped')
        self.assertEqual(status_lines([self.bot], 0), ["[kraken CRV/USD] STOPPED"])
        self.assertIn("🛑 kraken CRV/USD: STOPPED", summary_text([self.bot], 0))

    async def test_summary_after_one_trade(self):
        await self.do_one_long_trade()
        text = summary_text([self.bot], self.bot.last_cycle)
        # profit by hand: 0.01113 * 165.02 = 1.83667, fees 0.003 * 165.02 * 0.75041 = 0.37150
        # -> 1.46518 -> $1.47. After the trade: longs 0.36964 + 0.35884, short 0.38077 = 3 entries.
        self.assertIn("kraken CRV/USD\n"
                      "  1 done, $1.47\n"
                      "  waiting: 3 entries, 0 exits\n"
                      "  all time: 1 done, $1.47", text)
        self.assertIn("📊 ALL BOTS: 1 done, $1.47 | all time $1.47", text)

    def test_summary_warns_if_never_cycled(self):
        self.assertIn("⚠️ kraken CRV/USD: no cycle yet", summary_text([self.bot], 0))

    async def test_send_summary_resets_day_but_not_all_time(self):
        await self.do_one_long_trade()
        await send_summary([self.bot], self.bot.alert, self.bot.last_cycle)
        self.assertTrue(any('DAILY GRID SUMMARY' in a for a in self.alerts))
        self.assertEqual(self.bot.day_done, 0)
        self.assertEqual(self.bot.day_profit, 0.0)
        self.assertEqual(self.bot.completed, 1)                # all-time count kept

    # ---------- daily counts survive a restart ----------

    async def test_day_counts_survive_restart(self):
        await self.bot.recover()                   # fresh start (no state file yet)
        await self.do_one_long_trade()
        self.assertEqual(self.bot.day_done, 1)
        self.assertGreater(self.bot.day_profit, 0)

        new_bot = self.make_bot(self.levels)       # like Ctrl+C + shortcut
        await new_bot.recover()
        self.assertEqual(new_bot.day_done, 1)
        self.assertEqual(new_bot.day_profit, self.bot.day_profit)

    async def test_restart_after_summary_starts_day_at_zero(self):
        await self.bot.recover()
        await self.do_one_long_trade()
        await send_summary([self.bot], self.bot.alert, self.bot.last_cycle)

        new_bot = self.make_bot(self.levels)
        await new_bot.recover()
        self.assertEqual(new_bot.day_done, 0)
        self.assertEqual(new_bot.day_profit, 0.0)
        self.assertEqual(new_bot.completed, 1)     # all-time count still there

    async def test_old_state_without_day_counts(self):
        await self.bot.run_cycle()
        state = load_grid_state(self.state_file)
        del state['day_done'], state['day_profit']  # like a file saved before this fix
        save_grid_state(state, self.state_file)

        new_bot = self.make_bot(self.levels)
        await new_bot.recover()
        self.assertEqual(new_bot.day_done, 0)
        self.assertEqual(new_bot.day_profit, 0.0)

    async def test_summary_does_not_save_before_recover(self):
        # a bot that never recovered (e.g. it crashed on startup) must not
        # overwrite its state file with an empty trade list at 9 AM
        save_grid_state({'exchange': 'kraken', 'pair': 'CRV/USD', 'levels': self.levels,
                         'trades': [{'side': 'long'}], 'total_profit': 0,
                         'completed': 0}, self.state_file)
        await send_summary([self.bot], self.bot.alert, 0)
        self.assertEqual(load_grid_state(self.state_file)['trades'], [{'side': 'long'}])

    # ---------- mid-price ----------

    async def test_cycle_uses_mid_price_not_stale_last(self):
        # last trade 0.3795 is stale; the order book is at 0.3600 / 0.3610 -> mid 0.3605.
        # By hand, around 0.3605: buys 0.35884, 0.34835; sells 0.36964, 0.38077.
        # (Using last 0.3795 it would have been buys 0.36964, 0.35884 and sell 0.38077.)
        self.exchange.bid, self.exchange.ask = 0.3600, 0.3610
        await self.bot.run_cycle()
        prices = sorted((o['side'], o['price']) for o in self.exchange.fetch_open_orders('CRV/USD'))
        self.assertEqual(prices, [('buy', 0.34835), ('buy', 0.35884),
                                  ('sell', 0.36964), ('sell', 0.38077)])

    # ---------- errors file ----------

    def error_lines(self):
        if not os.path.exists(self.errors_file):
            return []
        with open(self.errors_file, encoding='utf-8') as f:
            return f.read().splitlines()

    async def run_briefly(self):
        """run_continuous for a moment; it never ends by itself while healthy."""
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(self.bot.run_continuous(), timeout=0.3)

    async def test_price_glitch_goes_to_errors_file(self):
        real = self.exchange.fetch_ticker
        calls = []

        def glitch_once(symbol):
            calls.append(symbol)
            if len(calls) == 1:
                raise ccxt.ExchangeError('kraken {"error":["EGeneral:No data"],"result":{}}')
            return real(symbol)
        self.exchange.fetch_ticker = glitch_once

        await self.run_briefly()
        lines = self.error_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(" | kraken CRV/USD | price check | Exchange glitch, retrying next cycle: ",
                      lines[0])
        self.assertIn("EGeneral:No data", lines[0])

    async def test_order_check_glitch_names_the_order(self):
        await self.bot.run_cycle()              # places buys 0.36964, 0.35884 and sell 0.38077
        real = self.exchange.fetch_order
        calls = []

        def glitch_once(order_id, symbol):
            calls.append(order_id)
            if len(calls) == 1:
                raise ccxt.ExchangeError('kraken {"error":["EGeneral:No data"],"result":{}}')
            return real(order_id, symbol)
        self.exchange.fetch_order = glitch_once

        await self.run_briefly()
        lines = self.error_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(" | order check (entry 0.36964) | ", lines[0])   # first trade checked

    async def test_stop_goes_to_errors_file(self):
        def broken(symbol):
            raise ccxt.ExchangeError("something new")
        self.exchange.fetch_ticker = broken

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ccxt.ExchangeError):
                await asyncio.wait_for(self.bot.run_continuous(), timeout=1)
        lines = self.error_lines()
        self.assertEqual(len(lines), 1)
        self.assertIn(" | price check | STOPPED - unexpected error: ExchangeError: something new",
                      lines[0])

    async def test_healthy_run_writes_nothing(self):
        await self.run_briefly()
        self.assertEqual(self.error_lines(), [])

    # ---------- exchange glitches ----------

    async def test_kraken_no_data_is_retried(self):
        calls = []
        real_ticker = self.exchange.fetch_ticker

        def glitch_once(symbol):
            calls.append(symbol)
            if len(calls) == 1:
                raise ccxt.ExchangeError('kraken {"error":["EGeneral:No data"],"result":{}}')
            return real_ticker(symbol)
        self.exchange.fetch_ticker = glitch_once

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(asyncio.TimeoutError):       # still running = not stopped
                await asyncio.wait_for(self.bot.run_continuous(), timeout=0.3)
        self.assertFalse(any('🛑' in a for a in self.alerts))
        # by hand: price 0.3795 -> buys 0.36964, 0.35884; only 0.38077 is above -> 1 sell = 3
        self.assertEqual(len(self.exchange.fetch_open_orders('CRV/USD')), 3)   # next cycle traded

    async def test_other_exchange_error_still_stops(self):
        def broken(symbol):
            raise ccxt.ExchangeError('kraken {"error":["EGeneral:Something new"],"result":{}}')
        self.exchange.fetch_ticker = broken

        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ccxt.ExchangeError):
                # timeout: if the bot wrongly retried forever, fail instead of hanging
                await asyncio.wait_for(self.bot.run_continuous(), timeout=1)
        self.assertEqual(self.bot.status, 'stopped')
        self.assertTrue(any('🛑 GRID BOT STOPPED - unexpected error' in a for a in self.alerts))

    async def test_helpers_stop_when_bots_stop(self):
        class BrokenExchange(FakeExchange):
            def fetch_ticker(self, symbol):
                raise RuntimeError("boom")
        broken = GridBot(BrokenExchange(price=0.3795), 'CAKE/USD', self.levels, 61, 2, 20,
                         self.bot.send, count=2, check_interval=0,
                         state_file='test_state_cake.json', log_file='test_log_cake.txt',
                         errors_file=self.errors_file)
        stopped = []

        async def helper():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                stopped.append(True)
                raise
        try:
            await asyncio.wait_for(run_bots([broken], helpers=[helper()]), timeout=1)
            await asyncio.sleep(0)              # let the cancel reach the helper
        finally:
            for f in ('test_state_cake.json', 'test_log_cake.txt'):
                if os.path.exists(f):
                    os.remove(f)
        self.assertEqual(stopped, [True])


    # ---------- held coins, low funds, funding check ----------

    async def test_summary_shows_held_long(self):
        await self.bot.run_cycle()
        buy = self.exchange.open_at('buy', 0.36964)
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        text = summary_text([self.bot], self.bot.last_cycle)
        # cost 165.02 * 0.36964 = 60.998 -> $61.00, now 165.02 * 0.3795 = 62.625 -> $62.63
        self.assertIn("  holding: 1 longs, 165.02 CRV, cost $61.00, now $62.63 (+$1.63)", text)
        self.assertIn("open trades if closed now: +$1.63", text)

    async def test_summary_shows_held_short(self):
        await self.bot.run_cycle()
        sell = self.exchange.open_at('sell', 0.38077)
        self.exchange.fill(sell['id'])
        await self.bot.run_cycle()
        text = summary_text([self.bot], self.bot.last_cycle)
        # sold 160.2 * 0.38077 = 60.999 -> $61.00, buy back 160.2 * 0.3795 = 60.796 -> $60.80
        self.assertIn("  shorts: 1, 160.2 CRV sold for $61.00, buy back now $60.80 (+$0.20)", text)

    async def test_low_funds_alerts_once_per_side(self):
        self.exchange.no_funds_for = {'buy'}
        await self.bot.run_cycle()
        await self.bot.run_cycle()
        low = [a for a in self.alerts if 'Not enough USD' in a]
        self.assertEqual(len(low), 1)                               # once, not every cycle
        self.assertIsNone(self.exchange.open_at('buy', 0.36964))    # no buys placed
        self.assertIsNotNone(self.exchange.open_at('sell', 0.38077))  # sells unaffected

        self.exchange.no_funds_for = set()                          # money arrives
        await self.bot.run_cycle()
        self.assertIsNotNone(self.exchange.open_at('buy', 0.36964))
        self.assertNotIn('long', self.bot.funds_alerted)            # ready to alert again

    def test_funding_report_adds_up_bots_on_same_exchange(self):
        rows = [('kraken', 'kraken A', 'USD', 'CRV', {'quote': 100, 'coin': 50}),
                ('kraken', 'kraken B', 'USD', 'CAKE', {'quote': 60, 'coin': 5})]
        balances = {'kraken': {'USD': 150, 'CRV': 50, 'CAKE': 4}}
        lines, short = funding_report(rows, balances)
        self.assertEqual(lines[0], "  kraken A: whole range needs 100.00 USD and 50.00 CRV")
        # USD: 100 + 60 = 160 > 150. CRV: 50 = 50, fine. CAKE: 5 > 4.
        self.assertEqual(short, ["kraken USD: need 160.00, have 150.00 (short 10.00)",
                                 "kraken CAKE: need 5.00, have 4.00 (short 1.00)"])

    def test_funding_report_missing_currency_counts_as_zero(self):
        rows = [('coinbase', 'c', 'USDC', 'CRV', {'quote': 10, 'coin': 0})]
        lines, short = funding_report(rows, {'coinbase': {}})
        self.assertEqual(short, ["coinbase USDC: need 10.00, have 0.00 (short 10.00)"])

    async def test_check_funding_alerts_when_short(self):
        # no trades yet, price 0.3795: longs at the 5 levels below = 5 * 61 = 305 USD,
        # short at 0.38077 = 61 / 0.38077 = 160.20 CRV
        self.exchange.balance = {'USD': 300, 'CRV': 1000}
        with contextlib.redirect_stdout(io.StringIO()):
            await check_funding([self.bot], self.bot.alert)
        funding = [a for a in self.alerts if 'FUNDING' in a]
        self.assertEqual(len(funding), 1)
        self.assertIn("kraken USD: need 305.00, have 300.00 (short 5.00)", funding[0])

    async def test_check_funding_quiet_when_funded(self):
        self.exchange.balance = {'USD': 305, 'CRV': 160.21}
        with contextlib.redirect_stdout(io.StringIO()):
            await check_funding([self.bot], self.bot.alert)
        self.assertEqual(self.alerts, [])

    async def test_check_funding_problem_does_not_stop_start(self):
        def broken():
            raise ccxt.NetworkError("down")
        self.exchange.fetch_balance = broken
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await check_funding([self.bot], self.bot.alert)        # must not raise
        self.assertIn("Funding check failed", out.getvalue())


    # ---------- out-of-range alert ----------
    # range in these tests: 0.32829 - 0.38077

    def range_alerts(self):
        return [a for a in self.alerts if 'RANGE' in a]

    async def test_inside_range_no_alert(self):
        await self.bot.run_cycle()
        await self.bot.run_cycle()
        self.assertEqual(self.range_alerts(), [])
        self.assertNotIn('OUT OF RANGE', self.bot.status_line)

    async def test_below_range_alerts_once_then_back(self):
        await self.bot.run_cycle()                  # 0.3795: inside
        self.exchange.price = 0.32                  # under 0.32829
        await self.bot.run_cycle()
        await self.bot.run_cycle()                  # still below: no second alert
        self.assertEqual(len(self.range_alerts()), 1)
        self.assertIn("price 0.32 is BELOW the range (0.32829)", self.range_alerts()[0])
        self.assertTrue(self.bot.status_line.endswith(" | OUT OF RANGE (below)"))
        self.assertIn("⚠️ kraken CRV/USD: OUT OF RANGE (below 0.32829)",
                      summary_text([self.bot], self.bot.last_cycle))

        self.exchange.price = 0.3795                # back inside
        await self.bot.run_cycle()
        self.assertEqual(len(self.range_alerts()), 2)
        self.assertIn("✅ BACK IN RANGE: price 0.3795 (0.32829–0.38077)", self.range_alerts()[1])
        self.assertNotIn('OUT OF RANGE', self.bot.status_line)

    async def test_above_range_alerts(self):
        self.exchange.price = 0.39                  # over 0.38077, already at the first cycle
        await self.bot.run_cycle()
        self.assertEqual(len(self.range_alerts()), 1)
        self.assertIn("price 0.39 is ABOVE the range (0.38077)", self.range_alerts()[0])


    # ---------- completed trades CSV ----------

    def read_csv(self):
        with open(self.csv_file, newline='') as f:
            return list(csv.DictReader(f))

    async def test_trade_done_writes_csv_row(self):
        await self.do_one_long_trade()
        rows = self.read_csv()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row['exchange'], row['pair'], row['side']), ('kraken', 'CRV/USD', 'long'))
        self.assertEqual((row['entry_price'], row['exit_price'], row['amount']),
                         ('0.36964', '0.38077', '165.02'))
        # by hand: 0.01113 * 165.02 = 1.83667, fees 0.003 * 165.02 * 0.75041 = 0.37150
        # -> 1.46518 -> 1.4652 (4 decimals)
        self.assertEqual(row['profit'], '1.4652')
        self.assertEqual(row['fees'], '0.3715')

    async def test_second_trade_adds_row_not_header(self):
        await self.do_one_long_trade()
        buy = self.exchange.open_at('buy', 0.36964)     # the long is placed again after the trade
        self.exchange.fill(buy['id'])
        await self.bot.run_cycle()
        self.exchange.fill(self.exchange.open_at('sell', 0.38077)['id'])
        await self.bot.run_cycle()
        with open(self.csv_file) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)                 # header + 2 trades
        self.assertTrue(lines[0].startswith('time,exchange,pair'))
        self.assertEqual(len(self.read_csv()), 2)

    def test_default_csv_name_includes_exchange_and_pair(self):
        async def alert(message):
            pass
        bot = GridBot(self.exchange, 'CRV/USD', self.levels, 61, 2, 20, alert,
                      errors_file=self.errors_file)
        self.assertEqual(bot.csv_file, 'grid_completed_kraken_CRV_USD.csv')


if __name__ == '__main__':
    unittest.main()
