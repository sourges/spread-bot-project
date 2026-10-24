import os
import unittest
from unittest.mock import patch
from grid_state import save_grid_state, load_grid_state

real_replace = os.replace


class SaveRetryTest(unittest.TestCase):
    """Windows can refuse the file swap for a moment ("Access is denied")."""

    def setUp(self):
        self.filename = 'test_retry_state.json'

    def tearDown(self):
        for f in (self.filename, self.filename + '.tmp'):
            if os.path.exists(f):
                os.remove(f)

    def test_locked_twice_then_saves(self):
        calls = []

        def flaky_replace(src, dst):
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError("[WinError 5] Access is denied")
            real_replace(src, dst)

        with patch('grid_state.os.replace', flaky_replace), patch('grid_state.time.sleep'):
            save_grid_state({'trades': [1]}, self.filename)
        self.assertEqual(len(calls), 3)                     # 2 refused + 1 that worked
        self.assertEqual(load_grid_state(self.filename), {'trades': [1]})

    def test_locked_every_time_still_raises(self):
        def always_locked(src, dst):
            raise PermissionError("[WinError 5] Access is denied")

        with patch('grid_state.os.replace', always_locked), patch('grid_state.time.sleep'):
            with self.assertRaises(PermissionError):
                save_grid_state({'trades': [1]}, self.filename)


if __name__ == '__main__':
    unittest.main()
