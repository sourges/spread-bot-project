import unittest
import os
from state import save_state, load_state

class JsonTestCase(unittest.TestCase):

    def setUp(self):
        self.filename = 'test_state.json'

    def test_load_missing_files(self):
        saved_trades = load_state(self.filename)
        self.assertIsNone(saved_trades)

    def test_save_state(self):
        save_state('buy', 'O6YHBH-4ATPI-7GSK2N', self.filename)
        saved_trades = load_state(self.filename)
        expected = {"waiting_for": "buy", "order_id": "O6YHBH-4ATPI-7GSK2N"}
        self.assertEqual(saved_trades, expected )

    def test_save_overwrite(self):
        save_state('buy', 'O6YHBH-4ATPI-7GSK2N', self.filename)
        save_state('sell', 'O6asdfasdfsdfsd', self.filename)
        saved_trades = load_state(self.filename)
        expected = {"waiting_for": "sell", "order_id": "O6asdfasdfsdfsd"}
        self.assertEqual(saved_trades, expected)

    def tearDown(self):
        if os.path.exists(self.filename):
            os.remove(self.filename)
        


if __name__ == '__main__':
    unittest.main()

# {"waiting_for": "sell", "order_id": "O6YHBH-4ATPI-7GSK2N"}