import contextlib
import io
import os
import unittest
from unittest.mock import patch
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from spread_bot import SpreadBot
from state import save_totals, load_totals, save_state, load_state


class FakeExchange:
    """Just enough of Kraken for the spread bot: orders live in a dict."""

    id = 'kraken'

    def __init__(self):
        self.orders = {}
        self.placed = []            # (side, amount, price) of every order the bot placed
        self.order_trades = {}      # order id -> its trades (exact price and cost)

    def fetch_order(self, order_id, symbol):
        return dict(self.orders[order_id])

    def create_limit_buy_order(self, symbol, amount, price):
        self.placed.append(('buy', amount, price))
        return {'id': f"B{len(self.placed)}"}

    def create_limit_sell_order(self, symbol, amount, price):
        self.placed.append(('sell', amount, price))
        return {'id': f"S{len(self.placed)}"}

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
                        compound=compound)

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
            await bot.place_buy_order()
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
            await self.bot.place_sell_order()

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


if __name__ == '__main__':
    unittest.main()
