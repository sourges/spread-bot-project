import contextlib
import io
import unittest
import ccxt
from order_cost import trades_cost


class FakeKraken:
    """fetch_order_trades returns whatever trades the test sets up."""
    id = 'kraken'

    def __init__(self, trades=None, error=None):
        self.trades = trades or []
        self.error = error
        self.calls = []

    def fetch_order_trades(self, order_id, symbol, params=None):
        self.calls.append((order_id, symbol, params))
        if self.error:
            raise self.error
        return self.trades


# Your real buy from Oct 30: 5004.21181 USDG @ 0.9999, Kraken cost 5003.71138882.
# Kraken reported the order's average as 0.9998 (cut off), so filled x average
# would be 5004.21181 x 0.9998 = 5003.21096... - half a dollar short.
REAL_ORDER = {'id': 'OCJD4F-FFKRE-VVHFCE', 'filled': 5004.21181, 'average': 0.9998,
              'trades': [{'id': 'T1'}]}


class TradesCostTest(unittest.TestCase):

    def test_real_usdg_buy_uses_trade_cost(self):
        kraken = FakeKraken([{'amount': 5004.21181, 'cost': 5003.71138882}])
        self.assertEqual(trades_cost(kraken, REAL_ORDER, 'USDG/USD'), 5003.71138882)
        self.assertEqual(kraken.calls, [('OCJD4F-FFKRE-VVHFCE', 'USDG/USD',
                                         {'trades': [{'id': 'T1'}]})])

    def test_several_trades_are_added(self):
        # by hand: 100 + 200.5 = 300.5, amounts 100 + 200 = 300 = filled
        order = {'id': 'O1', 'filled': 300, 'trades': [{'id': 'T1'}, {'id': 'T2'}]}
        kraken = FakeKraken([{'amount': 100, 'cost': 100.0}, {'amount': 200, 'cost': 200.5}])
        self.assertEqual(trades_cost(kraken, order, 'USDG/USD'), 300.5)

    def test_numbers_as_text(self):
        kraken = FakeKraken([{'amount': '5004.21181', 'cost': '5003.71138882'}])
        self.assertEqual(trades_cost(kraken, REAL_ORDER, 'USDG/USD'), 5003.71138882)

    def test_coinbase_is_not_asked(self):
        class FakeCoinbase(FakeKraken):
            id = 'coinbase'
        coinbase = FakeCoinbase([{'amount': 1, 'cost': 1}])
        self.assertIsNone(trades_cost(coinbase, REAL_ORDER, 'UNI/USDC'))
        self.assertEqual(coinbase.calls, [])

    def test_order_without_trades_list(self):
        kraken = FakeKraken([{'amount': 1, 'cost': 1}])
        order = {'id': 'O1', 'filled': 1}
        self.assertIsNone(trades_cost(kraken, order, 'USDG/USD'))
        self.assertEqual(kraken.calls, [])

    def test_trades_missing_some_amount(self):
        # trades add up to 5000, order filled 5004.21181: a trade is missing
        kraken = FakeKraken([{'amount': 5000, 'cost': 4999.5}])
        self.assertIsNone(trades_cost(kraken, REAL_ORDER, 'USDG/USD'))

    def test_trade_without_cost(self):
        kraken = FakeKraken([{'amount': 5004.21181, 'cost': None}])
        self.assertIsNone(trades_cost(kraken, REAL_ORDER, 'USDG/USD'))

    def test_network_problem_is_raised(self):
        kraken = FakeKraken(error=ccxt.NetworkError("timed out"))
        with self.assertRaises(ccxt.NetworkError):
            trades_cost(kraken, REAL_ORDER, 'USDG/USD')

    def test_other_error_falls_back(self):
        kraken = FakeKraken(error=ccxt.ExchangeError("EGeneral:Invalid arguments"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(trades_cost(kraken, REAL_ORDER, 'USDG/USD'))
        self.assertIn("using the order's average", out.getvalue())


if __name__ == '__main__':
    unittest.main()
