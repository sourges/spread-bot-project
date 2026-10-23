import os
import unittest
from grid_state import save_grid_state, load_grid_state
from reset_profit import reset_profit


class ResetProfitTest(unittest.TestCase):

    def setUp(self):
        self.state_file = 'test_reset_state.json'
        self.log_file = 'test_reset_log.txt'
        self.trades = [{'side': 'long', 'status': 'exit_open', 'entry_price': 0.36}]
        save_grid_state({'exchange': 'kraken', 'pair': 'CRV/USD', 'levels': [0.3, 0.36, 0.4],
                         'trades': self.trades, 'total_profit': 16.2384, 'completed': 14},
                        self.state_file)

    def tearDown(self):
        for f in (self.state_file, self.log_file):
            if os.path.exists(f):
                os.remove(f)

    def test_zeroes_profit_and_count(self):
        result = reset_profit(self.state_file, self.log_file, '2026-10-22 16:40:00')
        self.assertEqual(result, (16.2384, 14))
        state = load_grid_state(self.state_file)
        self.assertEqual(state['total_profit'], 0.0)
        self.assertEqual(state['completed'], 0)

    def test_keeps_trades_and_levels(self):
        reset_profit(self.state_file, self.log_file, '2026-10-22 16:40:00')
        state = load_grid_state(self.state_file)
        self.assertEqual(state['trades'], self.trades)
        self.assertEqual(state['levels'], [0.3, 0.36, 0.4])

    def test_writes_old_totals_to_log(self):
        reset_profit(self.state_file, self.log_file, '2026-10-22 16:40:00')
        with open(self.log_file) as f:
            self.assertEqual(f.read(), "2026-10-22 16:40:00 - PROFIT RESET (reinvested): "
                                       "was $16.2384, 14 trades\n")

    def test_log_keeps_earlier_lines(self):
        with open(self.log_file, 'w') as f:
            f.write("old trade line\n")
        reset_profit(self.state_file, self.log_file, '2026-10-22 16:40:00')
        with open(self.log_file) as f:
            self.assertTrue(f.read().startswith("old trade line\n"))

    def test_missing_state_file_changes_nothing(self):
        with self.assertRaises(FileNotFoundError):
            reset_profit('no_such_state.json', self.log_file, '2026-10-22 16:40:00')
        self.assertFalse(os.path.exists(self.log_file))


if __name__ == '__main__':
    unittest.main()
