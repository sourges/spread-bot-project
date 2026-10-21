import asyncio
import contextlib
import io
import os
import unittest
import ccxt
from grid_bot import GridBot, OrderProblem, run_bots
from grid_state import load_grid_state, save_grid_state


class FakeExchange:
    """Pretends to be Kraken. Orders live in a dict; tests fill them by hand."""

    id = 'kraken'

    def __init__(self, price):
        self.price = price
        self.orders = {}
        self.next_id = 1
        self.fill_on_cancel = set()     # ids that "fill just before the cancel arrives"

    def create_order(self, symbol, type, side, amount, price, params=None):
        order_id = f"O{self.next_id}"
        self.next_id += 1
        self.orders[order_id] = {
            'id': order_id, 'side': side, 'amount': amount, 'price': price,
            'status': 'open', 'filled': 0.0, 'cost': 0.0, 'average': None,
            'fee': None, 'params': params or {},
        }
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
        return {'last': self.price}

    def fetch_open_orders(self, symbol):
        return [dict(o) for o in self.orders.values() if o['status'] == 'open']

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


class GridBotTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        self.state_file = 'test_grid_state.json'
        self.log_file = 'test_grid_trades.txt'
        self.exchange = FakeExchange(price=0.3795)
        self.alerts = []
        self.bot = self.make_bot(self.levels)

    def make_bot(self, levels):
        async def alert(message):
            self.alerts.append(message)
        return GridBot(self.exchange, 'CRV/USD', levels, 61, 2, 20, alert,
                       count=2, check_interval=0,
                       state_file=self.state_file, log_file=self.log_file)

    def tearDown(self):
        for f in (self.state_file, self.log_file):
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
        self.assertEqual(exit_sell['params'], {})            # exits are not post-only
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
        bot = GridBot(self.exchange, 'CRV/USD', self.levels, 61, 2, 20, alert)
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
                         state_file='test_state_cake.json', log_file='test_log_cake.txt')
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


if __name__ == '__main__':
    unittest.main()
