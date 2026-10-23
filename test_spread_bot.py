import contextlib
import io
import os
import unittest
from unittest.mock import patch
from datetime import datetime
from spread_bot import SpreadBot
from state import save_totals, load_totals


class FakeExchange:
    """Just enough of Kraken for the spread bot: orders live in a dict."""

    id = 'kraken'

    def __init__(self):
        self.orders = {}

    def fetch_order(self, order_id, symbol):
        return dict(self.orders[order_id])


class SpreadBotTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.totals_file = 'test_spread_totals.json'
        self.exchange = FakeExchange()
        self.alerts = []
        self.bot = self.make_bot()

    def make_bot(self):
        bot = SpreadBot(self.exchange, 'USDG/USD', 0.9999, 1.0000, 100,
                        telegram_token='123:fake', chat_id='1', check_interval=0,
                        totals_file=self.totals_file)

        async def alert(message):
            self.alerts.append(message)
        bot.send_alert = alert
        return bot

    def tearDown(self):
        if os.path.exists(self.totals_file):
            os.remove(self.totals_file)

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

    def test_restart_keeps_totals(self):
        save_totals(0.37, 37, self.totals_file)
        bot = self.make_bot()
        self.assertEqual(bot.total_profit, 0.37)
        self.assertEqual(bot.total_trades, 37)

    def test_no_totals_file_starts_at_zero(self):
        self.assertEqual(load_totals('no_such_totals.json'), (0.0, 0))


if __name__ == '__main__':
    unittest.main()
