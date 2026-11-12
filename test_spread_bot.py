import contextlib
import io
import os
import unittest
import ccxt
from unittest.mock import patch
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from spread_bot import SpreadBot, OrderProblem, step_price
from state import save_totals, load_totals, save_state, load_state


class FakeExchange:
    """Just enough of Kraken for the spread bot: orders live in a dict."""

    id = 'kraken'

    def __init__(self):
        self.orders = {}
        self.placed = []            # (side, amount, price) of every order the bot placed
        self.params = []            # params of every order (to check postOnly)
        self.order_trades = {}      # order id -> its trades (exact price and cost)
        # What happens to the next orders placed, in order:
        #   'error'  -> Kraken refuses: "EOrder:Post only order"
        #   'cancel' -> accepted, then cancelled at once (nothing filled)
        #   'fail'   -> some other error (e.g. no funds)
        # empty list -> the order rests on the book ('open')
        self.outcomes = []
        self.ticker = {'bid': 0.9999, 'ask': 1.0, 'last': 1.0}
        self.cancelled = []
        self.min_amount = 1.0

    def fetch_order(self, order_id, symbol):
        return dict(self.orders[order_id])

    def create_order(self, symbol, type, side, amount, price, params=None):
        self.placed.append((side, amount, price))
        self.params.append(params)
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome == 'error':
            raise ccxt.InvalidOrder('kraken {"error":["EOrder:Post only order"]}')
        if outcome == 'fail':
            raise ccxt.InsufficientFunds('kraken {"error":["EOrder:Insufficient funds"]}')
        order_id = f"{'B' if side == 'buy' else 'S'}{len(self.placed)}"
        status = 'canceled' if outcome == 'cancel' else 'open'
        self.orders[order_id] = {'id': order_id, 'status': status, 'filled': 0}
        return {'id': order_id}

    def fetch_ticker(self, symbol):
        return dict(self.ticker)

    def cancel_order(self, order_id, symbol):
        self.cancelled.append(order_id)
        if self.orders[order_id]['status'] == 'closed':
            raise ccxt.OrderNotFound('already closed')
        self.orders[order_id]['status'] = 'canceled'

    def market(self, symbol):
        return {'limits': {'amount': {'min': self.min_amount}}}

    def load_markets(self):
        pass

    def fetch_order_trades(self, order_id, symbol, params=None):
        return self.order_trades[order_id]

    def amount_to_precision(self, symbol, amount):
        # like Kraken: round DOWN to 5 decimals, returned as text
        return str(Decimal(str(amount)).quantize(Decimal('0.00001'), rounding=ROUND_DOWN))


class SpreadBotTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.totals_file = 'test_spread_totals.json'
        self.state_file = 'test_spread_state.json'
        self.exchange = FakeExchange()
        self.alerts = []
        self.bot = self.make_bot()

    def make_bot(self, compound=False):
        bot = SpreadBot(self.exchange, 'USDG/USD', 0.9999, 1.0000, 100,
                        telegram_token='123:fake', chat_id='1', check_interval=0,
                        totals_file=self.totals_file, state_file=self.state_file,
                        compound=compound, confirm_delay=0)

        async def alert(message):
            self.alerts.append(message)
        bot.send_alert = alert
        return bot

    def tearDown(self):
        for f in (self.totals_file, self.state_file):
            if os.path.exists(f):
                os.remove(f)

    # ---------- compounding: buy_amount() ----------

    def test_compound_off_ignores_profit(self):
        self.bot.total_profit = 3.0
        self.assertEqual(self.bot.buy_amount(), 100)

    def test_compound_on_no_profit_yet(self):
        bot = self.make_bot(compound=True)
        bot.total_profit = 0.0
        self.assertEqual(bot.buy_amount(), 100)

    def test_compound_adds_profit_as_coins(self):
        # $3.00 / 0.9999 = 3.00030003 coins -> 103.00030003 -> 5 decimals down = 103.0003
        bot = self.make_bot(compound=True)
        bot.total_profit = 3.0
        self.assertEqual(bot.buy_amount(), 103.0003)

    def test_compound_rounds_down(self):
        # 0.0123456 / 0.9999 = 0.0123468347 -> 100.0123468 -> down 100.01234 (up would be ...35)
        bot = self.make_bot(compound=True)
        bot.total_profit = 0.0123456
        self.assertEqual(bot.buy_amount(), 100.01234)

    def test_compound_negative_profit_keeps_amount(self):
        bot = self.make_bot(compound=True)
        bot.total_profit = -0.5
        self.assertEqual(bot.buy_amount(), 100)

    async def test_buy_order_uses_compounded_amount(self):
        bot = self.make_bot(compound=True)
        bot.total_profit = 3.0
        with contextlib.redirect_stdout(io.StringIO()):
            await bot.place_order('buy')
        self.assertEqual(self.exchange.placed, [('buy', 103.0003, 0.9999)])
        self.assertIn("Buy 103.0003 USDG", self.alerts[-1])

    def test_status_line(self):
        line = self.bot.status_line('buy', 0.9999, datetime(2026, 10, 23, 10, 43, 12))
        self.assertEqual(line, "[kraken USDG/USD] 10:43:12 | waiting for buy @ 0.9999 | "
                               "done: 0 | profit $0.0000")

    async def test_open_buy_prints_status_line(self):
        self.exchange.orders['B1'] = {'status': 'open', 'filled': 0}
        self.bot.buy_order_id = 'B1'
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            filled = await self.bot.check_buy_filled()
        self.assertFalse(filled)
        self.assertIn("[kraken USDG/USD]", out.getvalue())
        self.assertIn("| waiting for buy @ 0.9999 | done: 0 | profit $0.0000", out.getvalue())

    async def test_sell_fill_counts_and_saves_totals(self):
        self.exchange.orders['S1'] = {'status': 'closed', 'filled': 100, 'average': 1.0}
        self.bot.sell_order_id = 'S1'
        # patch: don't write a fake line into the real trades.txt
        with patch('spread_bot.log_trade'), contextlib.redirect_stdout(io.StringIO()):
            await self.bot.check_sell_filled()
        # by hand: sold 100 @ 1.0000 = 100.00, bought 100 @ 0.9999 = 99.99 -> 0.01
        self.assertAlmostEqual(self.bot.total_profit, 0.01)
        self.assertEqual(self.bot.total_trades, 1)
        profit, trades = load_totals(self.totals_file)
        self.assertAlmostEqual(profit, 0.01)
        self.assertEqual(trades, 1)

    async def test_profit_uses_exact_trade_cost_not_cut_off_average(self):
        # Your Oct 30 trade: 5003.21106 USDG bought @ 0.9999, sold @ 1.0.
        # Kraken said average 0.9998 on the buy; the trade itself says 0.9999.
        # by hand: cost 5003.21106 x 0.9999 = 5002.710738894
        #          revenue 5003.21106 x 1.0 = 5003.21106
        #          profit = 5003.21106 x 0.0001 = 0.500321106 (the bot said 1.0006)
        self.exchange.orders['B1'] = {'id': 'B1', 'status': 'closed', 'filled': 5003.21106,
                                      'average': 0.9998, 'cost': 5003.21106 * 0.9998,
                                      'trades': [{'id': 'TB'}]}
        self.exchange.order_trades['B1'] = [{'amount': 5003.21106, 'cost': 5002.710738894}]
        self.exchange.orders['S1'] = {'id': 'S1', 'status': 'closed', 'filled': 5003.21106,
                                      'average': 1.0, 'cost': 5003.21106,
                                      'trades': [{'id': 'TS'}]}
        self.exchange.order_trades['S1'] = [{'amount': 5003.21106, 'cost': 5003.21106}]
        self.bot.buy_order_id = 'B1'
        with patch('spread_bot.log_trade'), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await self.bot.check_buy_filled())
            self.bot.sell_order_id = 'S1'
            self.assertTrue(await self.bot.check_sell_filled())
        self.assertAlmostEqual(self.bot.total_profit, 0.500321106, places=8)

    def test_restart_keeps_totals(self):
        save_totals(0.37, 37, self.totals_file)
        bot = self.make_bot()
        self.assertEqual(bot.total_profit, 0.37)
        self.assertEqual(bot.total_trades, 37)

    def test_no_totals_file_starts_at_zero(self):
        self.assertEqual(load_totals('no_such_totals.json'), (0.0, 0))


    # ---------- sell and profit use what really filled ----------

    async def fill_buy(self, order):
        """Pretend the bot's buy B1 filled like `order`, then let it place the sell."""
        self.exchange.orders['B1'] = order
        self.bot.buy_order_id = 'B1'
        with patch('spread_bot.log_trade'), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await self.bot.check_buy_filled())
            await self.bot.place_order('sell')

    async def fill_sell(self, order):
        self.exchange.orders['S1'] = order
        self.bot.sell_order_id = 'S1'
        with patch('spread_bot.log_trade'), contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(await self.bot.check_sell_filled())

    async def test_sell_is_what_the_buy_filled(self):
        await self.fill_buy({'status': 'closed', 'filled': 100, 'average': 0.9999,
                             'cost': 99.99, 'fee': {'cost': 0.0, 'currency': 'USD'}})
        self.assertEqual(self.exchange.placed, [('sell', 100, 1.0)])
        saved = load_state(self.state_file)
        self.assertEqual(saved['waiting_for'], 'sell')
        self.assertEqual(saved['buy'], {'amount': 100, 'cost': 99.99, 'fee_usd': 0.0,
                                        'sell_amount': 100})

    async def test_changed_amount_does_not_change_the_sell(self):
        self.bot.amount = 200                       # AMOUNT raised, but this buy was for 100
        await self.fill_buy({'status': 'closed', 'filled': 100, 'average': 0.9999,
                             'cost': 99.99, 'fee': None})
        self.assertEqual(self.exchange.placed, [('sell', 100, 1.0)])

    async def test_fee_taken_in_coins_is_not_sold(self):
        await self.fill_buy({'status': 'closed', 'filled': 100, 'average': 0.9999,
                             'cost': 99.99, 'fee': {'cost': 0.02, 'currency': 'USDG'}})
        self.assertEqual(self.exchange.placed, [('sell', 99.98, 1.0)])    # 100 - 0.02
        self.assertAlmostEqual(self.bot.buy_fill['fee_usd'], 0.019998)   # 0.02 * 0.9999

    async def test_profit_subtracts_both_fees(self):
        await self.fill_buy({'status': 'closed', 'filled': 100, 'average': 0.9999,
                             'cost': 99.99, 'fee': {'cost': 0.10, 'currency': 'USD'}})
        await self.fill_sell({'status': 'closed', 'filled': 100, 'average': 1.0,
                              'cost': 100.0, 'fee': {'cost': 0.10, 'currency': 'USD'}})
        # by hand: 100.00 - 99.99 - 0.10 - 0.10 = -0.19
        self.assertAlmostEqual(self.bot.total_profit, -0.19)

    async def test_restart_while_selling_uses_saved_buy(self):
        # saved buy: cost 99.95 + fee 0.01 (different from 100 * 0.9999, so we can tell)
        save_state('sell', 'S1', self.state_file,
                   buy={'amount': 100, 'cost': 99.95, 'fee_usd': 0.01, 'sell_amount': 100})
        self.bot.amount = 200                       # changed while it was down
        await self.bot.recover()
        await self.fill_sell({'status': 'closed', 'filled': 100, 'average': 1.0,
                              'cost': 100.0, 'fee': None})
        # by hand: 100.00 - 99.95 - 0.01 = 0.04 (old math: 100 - 200 * 0.9999 = -99.98)
        self.assertAlmostEqual(self.bot.total_profit, 0.04)

    async def test_old_state_without_buy_still_works(self):
        save_state('sell', 'S1', self.state_file)  # saved before this fix
        self.bot.amount = 200
        await self.bot.recover()
        await self.fill_sell({'status': 'closed', 'filled': 100, 'average': 1.0,
                              'cost': 100.0, 'fee': None})
        # falls back to sold amount * buy price: 100 - 100 * 0.9999 = 0.01
        self.assertAlmostEqual(self.bot.total_profit, 0.01)



class PostOnlyStepTest(unittest.IsolatedAsyncioTestCase):
    """Post-only orders, 5 tries per price, one step further, hourly move back.
    Normal prices: buy 0.9999, sell 1.0. One step = 0.0001."""

    def setUp(self):
        self.totals_file = 'test_spread_totals.json'
        self.state_file = 'test_spread_state.json'
        self.exchange = FakeExchange()
        self.alerts = []
        self.bot = self.make_bot()

    def make_bot(self):
        bot = SpreadBot(self.exchange, 'USDG/USD', 0.9999, 1.0000, 100,
                        telegram_token='123:fake', chat_id='1', check_interval=0,
                        totals_file=self.totals_file, state_file=self.state_file,
                        confirm_delay=0)

        async def alert(message):
            self.alerts.append(message)
        bot.send_alert = alert
        return bot

    def tearDown(self):
        for f in (self.totals_file, self.state_file):
            if os.path.exists(f):
                os.remove(f)

    def bought_100(self):
        """As if a buy of 100 @ 0.9999 filled: the sell is for 100 coins."""
        self.bot.buy_fill = {'amount': 100, 'cost': 99.99, 'fee_usd': 0.0, 'sell_amount': 100}

    async def place(self, side):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            await self.bot.place_order(side)
        return out.getvalue()

    async def check(self, side):
        out = io.StringIO()
        with patch('spread_bot.log_trade'), contextlib.redirect_stdout(out):
            if side == 'buy':
                done = await self.bot.check_buy_filled()
            else:
                done = await self.bot.check_sell_filled()
        return done, out.getvalue()

    def prices(self):
        return [price for side, amount, price in self.exchange.placed]

    # ---------- helpers ----------

    def test_step_price(self):
        self.assertEqual(step_price(1.0, 'sell', 1, 0.0001), 1.0001)
        self.assertEqual(step_price(1.0, 'sell', 2, 0.0001), 1.0002)
        self.assertEqual(step_price(0.9999, 'buy', 1, 0.0001), 0.9998)
        self.assertEqual(step_price(0.9999, 'buy', 0, 0.0001), 0.9999)

    # ---------- placing ----------

    async def test_orders_are_post_only(self):
        await self.place('buy')
        self.assertEqual(self.exchange.placed, [('buy', 100, 0.9999)])
        self.assertEqual(self.exchange.params, [{'postOnly': True}])
        self.assertIn("🟢 BUY ORDER PLACED\nBuy 100 USDG at $0.9999", self.alerts[-1])

    async def test_sell_tries_5_times_then_steps_up(self):
        self.bought_100()
        self.exchange.outcomes = ['error'] * 5           # 1.0 rejected 5 times
        await self.place('sell')
        self.assertEqual(self.prices(), [1.0, 1.0, 1.0, 1.0, 1.0, 1.0001])
        self.assertEqual(self.bot.order_price, 1.0001)
        self.assertEqual(len(self.alerts), 1)            # one Telegram message, not 6
        self.assertIn("⚠️ SELL STEPPED UP to 1.0001 (normal 1.0)", self.alerts[0])

    async def test_buy_rejected_by_cancel_steps_down(self):
        self.exchange.outcomes = ['cancel'] * 5          # accepted, then cancelled at once
        await self.place('buy')
        self.assertEqual(self.prices(), [0.9999] * 5 + [0.9998])
        self.assertIn("⚠️ BUY STEPPED DOWN to 0.9998 (normal 0.9999)", self.alerts[0])

    async def test_keeps_stepping_5_tries_per_price(self):
        self.bought_100()
        self.exchange.outcomes = ['error'] * 10          # 1.0 x5, 1.0001 x5
        await self.place('sell')
        self.assertEqual(self.prices(), [1.0] * 5 + [1.0001] * 5 + [1.0002])

    async def test_screen_shows_each_rejection(self):
        self.bought_100()
        self.exchange.outcomes = ['error'] * 5
        screen = await self.place('sell')
        self.assertIn("sell @ 1.0 rejected (post-only: market already past it) - "
                      "try 3/5, next in 0s", screen)
        self.assertIn("try 5/5, next try at 1.0001", screen)

    async def test_other_failures_stop_after_5(self):
        self.exchange.outcomes = ['fail'] * 5
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(OrderProblem):
                await self.bot.place_order('buy')
        self.assertEqual(len(self.exchange.placed), 5)

    async def test_post_only_rejections_are_not_failures(self):
        # 4 real failures + 5 rejections: still fewer than 5 real failures -> no stop
        self.exchange.outcomes = ['fail'] * 4 + ['error'] * 5
        await self.place('buy')
        self.assertEqual(self.prices()[-1], 0.9998)

    async def test_stepped_status_line(self):
        self.bought_100()
        self.exchange.outcomes = ['error'] * 5
        await self.place('sell')
        line = self.bot.status_line('sell', self.bot.order_price, datetime(2026, 11, 12, 11, 58))
        self.assertIn("waiting for sell @ 1.0001  ⚠ STEPPED (normal 1.0) | "
                      "next check for 1.0 at 12:58 |", line)

    # ---------- moving back to the normal price ----------

    async def stepped_sell(self):
        self.bought_100()
        self.exchange.outcomes = ['error'] * 5
        await self.place('sell')                          # resting at 1.0001 as S6
        self.alerts.clear()

    async def test_not_due_yet_does_nothing(self):
        await self.stepped_sell()
        self.exchange.ticker = {'bid': 0.9999, 'ask': 1.0}
        done, _ = await self.check('sell')               # next check is an hour away
        self.assertFalse(done)
        self.assertEqual(self.exchange.cancelled, [])

    async def test_hourly_check_keeps_when_bid_still_at_or_above(self):
        await self.stepped_sell()
        self.bot.next_check = 0                          # due now
        self.exchange.ticker = {'bid': 1.0001, 'ask': 1.0002}
        done, screen = await self.check('sell')
        self.assertFalse(done)
        self.assertIn("checking 1.0: best bid 1.0001, still above - keeping 1.0001", screen)
        self.assertEqual(self.exchange.cancelled, [])
        self.assertGreater(self.bot.next_check, 0)       # next look in an hour

    async def test_hourly_check_moves_sell_back(self):
        await self.stepped_sell()
        self.bot.next_check = 0
        self.exchange.ticker = {'bid': 0.9999, 'ask': 1.0}  # nobody buying at 1.0: it can rest
        done, screen = await self.check('sell')
        self.assertFalse(done)
        self.assertIn("checking 1.0: best bid 0.9999 - moving sell back to 1.0", screen)
        self.assertEqual(self.exchange.cancelled, ['S6'])
        self.assertEqual(self.exchange.placed[-1], ('sell', 100, 1.0))
        self.assertEqual(self.bot.order_price, 1.0)
        self.assertFalse(self.bot.is_stepped())
        self.assertIn("↩️ SELL MOVED BACK to 1.0 (was 1.0001)", self.alerts[-1])

    async def test_hourly_check_moves_buy_back(self):
        self.exchange.outcomes = ['error'] * 5
        await self.place('buy')                          # resting at 0.9998 as B6
        self.bot.next_check = 0
        self.exchange.ticker = {'bid': 0.9999, 'ask': 1.0}  # ask above 0.9999: it can rest
        await self.check('buy')
        self.assertEqual(self.exchange.placed[-1], ('buy', 100, 0.9999))
        self.assertIn("↩️ BUY MOVED BACK to 0.9999 (was 0.9998)", self.alerts[-1])

    async def test_partly_filled_then_moved_back(self):
        # 40 of the 100 sold at 1.0001 before the move: by hand 40 x 1.0001 = 40.004
        # rest 60 sold at 1.0 = 60.0  -> revenue 100.004, buy cost 99.99, profit 0.014
        await self.stepped_sell()
        self.exchange.orders['S6'].update({'filled': 40, 'cost': 40.004, 'average': 1.0001})
        self.bot.next_check = 0
        self.exchange.ticker = {'bid': 0.9999, 'ask': 1.0}
        await self.check('sell')
        self.assertEqual(self.exchange.placed[-1], ('sell', 60.0, 1.0))   # only the rest
        new_id = self.bot.sell_order_id
        self.exchange.orders[new_id] = {'status': 'closed', 'filled': 60, 'cost': 60.0,
                                        'average': 1.0}
        done, _ = await self.check('sell')
        self.assertTrue(done)
        self.assertAlmostEqual(self.bot.total_profit, 0.014, places=8)
        self.assertIn("Sold: 100.0 USDG", self.alerts[-1])

    async def test_filled_just_before_cancel(self):
        # by hand: 100 x 1.0001 = 100.01, minus 99.99 -> profit 0.02
        await self.stepped_sell()
        self.bot.next_check = 0
        self.exchange.ticker = {'bid': 0.9999, 'ask': 1.0}

        def cancel(order_id, symbol):              # it filled in the meantime
            self.exchange.orders[order_id] = {'status': 'closed', 'filled': 100,
                                              'cost': 100.01, 'average': 1.0001}
            raise ccxt.OrderNotFound('already closed')
        self.exchange.cancel_order = cancel
        done, _ = await self.check('sell')
        self.assertTrue(done)
        self.assertAlmostEqual(self.bot.total_profit, 0.02, places=8)
        self.assertEqual(len(self.exchange.placed), 6)  # nothing re-placed

    async def test_next_cycle_starts_at_normal_price(self):
        await self.stepped_sell()
        self.exchange.orders['S6'] = {'status': 'closed', 'filled': 100, 'cost': 100.01,
                                      'average': 1.0001}
        done, _ = await self.check('sell')
        self.assertTrue(done)
        await self.place('buy')
        self.assertEqual(self.exchange.placed[-1], ('buy', 100, 0.9999))
        self.assertIn("🟢 BUY ORDER PLACED", self.alerts[-1])

    # ---------- restart and odd cases ----------

    async def test_restart_remembers_stepped_order(self):
        await self.stepped_sell()
        new_bot = self.make_bot()
        await new_bot.recover()
        self.assertEqual(new_bot.sell_order_id, 'S6')
        self.assertEqual(new_bot.order_price, 1.0001)
        self.assertEqual(new_bot.normal_price, 1.0)
        self.assertTrue(new_bot.is_stepped())
        self.assertIsNotNone(new_bot.next_check)

    async def test_post_only_cancel_later_is_placed_again(self):
        self.exchange.orders['B1'] = {'status': 'canceled', 'filled': 0,
                                      'info': {'reason': 'Post only order'}}
        self.bot.buy_order_id = 'B1'
        done, _ = await self.check('buy')
        self.assertFalse(done)
        self.assertEqual(self.exchange.placed, [('buy', 100, 0.9999)])

    async def test_other_cancel_still_stops(self):
        self.exchange.orders['B1'] = {'status': 'canceled', 'filled': 0,
                                      'info': {'reason': 'User requested'}}
        self.bot.buy_order_id = 'B1'
        with self.assertRaises(OrderProblem):
            await self.bot.check_buy_filled()

if __name__ == '__main__':
    unittest.main()
