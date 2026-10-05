import unittest
from grid import grid_levels, split_levels, centered_grid, tick_to_decimals

class LevelTest(unittest.TestCase):
    def test_five_levels(self):
        saved_levels = grid_levels(0.9990, 1.0010, 5)
        expected = [0.999, 0.9995, 1.0, 1.0005, 1.001]
        self.assertEqual(saved_levels, expected)

    def test_twentyone_levels(self):
        saved_levels = grid_levels(0.9980, 1.0020, 21)
        self.assertEqual(len(saved_levels), 21)
        self.assertEqual(saved_levels[0], 0.998)
        self.assertEqual(saved_levels[-1], 1.002)
            

    def test_one_levels(self):
        with self.assertRaises(ValueError):
            grid_levels(0.9990, 1.0010, 1)

    def test_split_price_on_level(self):
        levels = [0.999, 0.9995, 1.0, 1.0005, 1.001]
        result = split_levels(levels, 1.0)
        expected = {'buys': [0.999, 0.9995], 'sells': [1.0005, 1.001]}
        self.assertEqual(result, expected)

    def test_split_price_between_level(self):
        levels = [0.999, 0.9995, 1.0, 1.0005, 1.001]
        result = split_levels(levels, 1.0002)
        expected = {'buys': [0.999, 0.9995, 1.0], 'sells': [1.0005, 1.001]}
        self.assertEqual(result, expected)

    def test_split_price_below_grid(self):
        levels = [0.999, 0.9995, 1.0, 1.0005, 1.001]
        result = split_levels(levels, 0.99)
        expected = {'buys': [], 'sells': [0.999, 0.9995, 1.0, 1.0005, 1.001]}
        self.assertEqual(result, expected)

    def test_centered_grid(self):
        result = centered_grid(1, 0.05, 2)
        expected = [0.999, 0.9995, 1.0, 1.0005, 1.001]
        self.assertEqual(result, expected)

    def test_centered_middle(self):
        result = centered_grid(1, 0.05, 10)
        expected = 1.0
        self.assertEqual(result[10], expected)
        self.assertEqual(len(result), 21)

    def test_centered_grid_gives_10_buys_10_sells(self):
        levels = centered_grid(1.0, 0.02, levels_per_side=10)
        result = split_levels(levels, 1.0)
        self.assertEqual(len(result['buys']), 10)
        self.assertEqual(len(result['sells']), 10)

    def test_cake(self):
        levels = centered_grid(2.593, 1, levels_per_side=10, decimals=3)
        expected = 2.593
        self.assertEqual(len(levels), 21)
        self.assertEqual(levels[10], expected)

    def test_decimal_three(self):
        result = tick_to_decimals(0.001)
        self.assertEqual(result, 3)

    def test_decimal_five(self):
        result = tick_to_decimals(1e-05)
        self.assertEqual(result, 5)

    def test_decimal_four(self):
        result = tick_to_decimals(0.0001)
        self.assertEqual(result, 4)

    def test_decimal_zero(self):
        result = tick_to_decimals(1)
        self.assertEqual(result, 0)

if __name__ == '__main__':
    unittest.main()


