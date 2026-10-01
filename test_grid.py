import unittest
from grid import grid_levels, split_levels

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


if __name__ == '__main__':
    unittest.main()


