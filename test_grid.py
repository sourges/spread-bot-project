import unittest
from grid import grid_levels, split_levels, centered_grid, tick_to_decimals, geometric_levels, free_levels_below, free_levels_above, exit_order, neighbor_level, live_entries, plan_entries

from grid import order_amount, bot_name, check_config, open_positions, funding_needs


class LevelTest(unittest.TestCase):
    def setUp(self):
        self.long_live = {'side': 'long', 'status': 'entry_open', 'entry_price': 0.35884, 'exit_price': 0.36964}
        self.long_filled = {'side': 'long',  'status': 'exit_open',  'entry_price': 0.36964, 'exit_price': 0.38077}
        self.short_live  = {'side': 'short', 'status': 'entry_open', 'entry_price': 0.38077, 'exit_price': 0.36964}
        self.trades = [self.long_live, self.long_filled, self.short_live]
        self.levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]

    
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

    def test_geometric_levels(self):
        result = geometric_levels(1, 4, 3)
        expected = [1.0, 2.0, 4.0]
        self.assertEqual(result, expected)

    def test_geometric_crv(self):
        result = geometric_levels(0.1431, 1.1442, 71, decimals=5)
        length = len(result)
        self.assertEqual(length, 71)
        first_level = result[0]
        self.assertEqual(first_level, 0.1431)
        last_level = result[-1]
        self.assertEqual(last_level, 1.1442)
        self.assertAlmostEqual(result[1] / result[0], 1.0301, places=3)

    def test_live_buy_does_not_block_level(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        current_price = 0.3795
        expected = [0.35884, 0.34835]
        trades = [{'status': 'exit_open',  'entry_price': 0.36964, 'exit_price': 0.38077},
        {'status': 'entry_open', 'entry_price': 0.35884, 'exit_price': 0.36964}]
        result = free_levels_below(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)
    
    def test_free_levels_below(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        current_price = 0.3795
        expected = [0.35884, 0.34835]
        trades = [{'status': 'exit_open', 'entry_price': 0.36964, 'exit_price': 0.38077}]
        result = free_levels_below(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)

    def test_empty_trades(self):
        trades = []
        expected = [0.36964, 0.35884]
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        result = free_levels_below(levels, 0.3795, trades, count=2)
        self.assertEqual(result, expected)

    def test_bottom_of_grid(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        trades = []
        expected = [0.32829]
        result = free_levels_below(levels, 0.33, trades, count=2)
        self.assertEqual(result, expected)

    def test_above_empty_trades(self):
        current_price = 0.3505
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        trades = []
        expected = [0.35884, 0.36964]
        result = free_levels_above(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)

    def test_sell_levels(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        trades = [{'status': 'exit_open', 'entry_price': 0.35884, 'exit_price': 0.34835}]
        current_price = 0.3505
        expected = [0.36964, 0.38077]
        result = free_levels_above(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)

    def test_top_of_grid(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        trades = []
        current_price = 0.375
        expected = [0.38077]
        result = free_levels_above(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)

    def test_sell_does_not_block(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        trades = [{'status': 'entry_open', 'entry_price': 0.35884, 'exit_price': 0.34835}]
        current_price = 0.3505
        expected = [0.35884, 0.36964]
        result = free_levels_above(levels, current_price, trades, count=2)
        self.assertEqual(result, expected)

    def test_long_exit(self):
        expected = {'side': 'sell', 'price': 0.38077, 'amount': 164.67}
        trade = {'side': 'long', 'entry_price': 0.36964, 'exit_price': 0.38077}
        filled_amount=164.67
        result = exit_order(trade, filled_amount)
        self.assertEqual(result, expected)

    def test_short_exit(self):
        expected = {'side': 'buy', 'price': 0.36964, 'amount': 160.20}
        trade = {'side': 'short', 'entry_price': 0.38077, 'exit_price': 0.36964}
        filled_amount = 160.20
        result = exit_order(trade, filled_amount)
        self.assertEqual(result, expected)

    def test_exit_value_error(self):
        trade = {'side': 'lng', 'entry_price': 0.38077, 'exit_price': 0.36964}
        filled_amount = 160.20
        with self.assertRaises(ValueError):
            exit_order(trade, filled_amount)

    def test_up_exit(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        expected = 0.38077
        result = neighbor_level(levels, 0.36964, 'up')
        self.assertEqual(result, expected)

    def test_down_exit(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        expected = 0.35884
        result = neighbor_level(levels, 0.36964, 'down')
        self.assertEqual(result, expected)

    def test_top_exit(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        result = neighbor_level(levels, 0.38077, 'up')
        self.assertIsNone(result)

    def test_bottom_exit(self):
            levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
            result = neighbor_level(levels, 0.32829, 'down')
            self.assertIsNone(result)

    def test_error_exit(self):
        levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
        with self.assertRaises(ValueError):
            neighbor_level(levels, 0.38077, 'sideways')

    def test_long_live_entries(self):
        result = live_entries(self.trades, 'long')
        self.assertEqual(result, [self.long_live])

    def test_short_live_entries(self):
        result = live_entries(self.trades, 'short')
        self.assertEqual(result, [self.short_live])

    def test_empty_live_entries(self):
        trades = []
        result = live_entries(trades, 'long')
        self.assertEqual(result, [])

    def test_plan_entries_worked_example(self):
        current_price = 0.3795
        trades = [
                {'side': 'long', 'entry_price': 0.35884, 'exit_price': 0.36964,
                 'amount': None, 'entry_id': 'A', 'exit_id': None,
                 'status': 'entry_open'},
                {'side': 'long', 'entry_price': 0.34835, 'exit_price': 0.35884,
                 'amount': None, 'entry_id': 'B', 'exit_id': None,
                 'status': 'entry_open'},]
        expected = {'place': [{'side': 'long', 'entry_price': 0.36964, 'exit_price': 0.38077, 'amount': None, 'entry_id': None, 'exit_id': None, 'status': 'entry_open'},
                              {'side': 'short', 'entry_price': 0.38077, 'exit_price': 0.36964, 'amount': None, 'entry_id': None, 'exit_id': None, 'status': 'entry_open'}], 
                              'cancel': [trades[1]]}
        result = plan_entries(self.levels, current_price, trades)
        self.assertEqual(result, expected)

    def test_plan_entries_nothing_to_do(self):
        current_price = 0.3795
        trades = [
                {'side': 'long', 'entry_price': 0.36964, 'exit_price': 0.38077,
                 'amount': None, 'entry_id': 'A', 'exit_id': None,
                 'status': 'entry_open'},
                {'side': 'long', 'entry_price': 0.35884, 'exit_price': 0.36964,
                 'amount': None, 'entry_id': 'B', 'exit_id': None,
                 'status': 'entry_open'},
                 {'side': 'short', 'entry_price': 0.38077, 'exit_price': 0.36964,
                 'amount': None, 'entry_id': 'C', 'exit_id': None,
                 'status': 'entry_open'}]
        expected = {'place': [], 'cancel': []}
        result = plan_entries(self.levels, current_price, trades)
        self.assertEqual(result, expected)

    def test_plan_entries_skips_top_level(self):
        current_price = 0.39
        trades = []

        expected = {
            'place': [
                {'side': 'long', 'entry_price': 0.36964, 'exit_price': 0.38077,
                 'amount': None, 'entry_id': None, 'exit_id': None,
                 'status': 'entry_open'},
            ],
            'cancel': [],
        }
        result = plan_entries(self.levels, current_price, trades)
        self.assertEqual(result, expected)

    def test_plan_entries_filled_trade_blocks_levels(self):
        current_price = 0.3795
        trades = [
            {'side': 'long', 'entry_price': 0.36964, 'exit_price': 0.38077,
            'amount': None, 'entry_id': 'A', 'exit_id': 'X',
            'status': 'exit_open'},
        ]
        # taken: 0.36964, 0.38077
        # long targets: 0.35884, 0.34835 (0.36964 skipped)
        # short targets: none (0.38077 taken)
        # filled trade isn't entry_open, so it can't be cancelled
        expected = {
            'place': [
                {'side': 'long', 'entry_price': 0.35884, 'exit_price': 0.36964,
                'amount': None, 'entry_id': None, 'exit_id': None,
                'status': 'entry_open'},
                {'side': 'long', 'entry_price': 0.34835, 'exit_price': 0.35884,
                'amount': None, 'entry_id': None, 'exit_id': None,
                'status': 'entry_open'},
            ],
            'cancel': [],
        }
        result = plan_entries(self.levels, current_price, trades)
        self.assertEqual(result, expected)

    def test_order_amount_rounds_down(self):
        # 61 / 0.35 = 174.2857... → round DOWN to 2 decimals → 174.28
        result = order_amount(61, 0.35, 2, 20)
        expected = 174.28
        self.assertEqual(result, expected)

    def test_order_amount_below_minimum(self):
        # 5 / 0.35 = 14.28 CRV, below the 20 CRV minimum
        with self.assertRaises(ValueError):
            order_amount(5, 0.35, 2, 20)

    def test_bot_name(self):
        self.assertEqual(bot_name('kraken', 'CRV/USD'), 'kraken_CRV_USD')
        self.assertEqual(bot_name('coinbase', 'CRV/USDC'), 'coinbase_CRV_USDC')

    def test_check_config_accepts_good_bots(self):
        bots = [{'exchange': 'kraken', 'pair': 'CRV/USD', 'lower': 0.14, 'upper': 1.14,
                 'levels': 71, 'usd_per_entry': 61, 'count': 2},
                {'exchange': 'coinbase', 'pair': 'CRV/USD', 'lower': 0.2, 'upper': 1.2,
                 'levels': 66, 'usd_per_entry': 50, 'count': 2}]
        check_config(bots)          # same pair on two exchanges is fine; no error = pass

    def test_check_config_rejects_duplicate(self):
        bot = {'exchange': 'kraken', 'pair': 'CRV/USD', 'lower': 0.14, 'upper': 1.14,
               'levels': 71, 'usd_per_entry': 61, 'count': 2}
        with self.assertRaises(ValueError):
            check_config([bot, dict(bot)])

    def test_check_config_rejects_bad_values(self):
        good = {'exchange': 'kraken', 'pair': 'CRV/USD', 'lower': 0.14, 'upper': 1.14,
                'levels': 71, 'usd_per_entry': 61, 'count': 2}
        bad_changes = [{'lower': 2.0},          # lower above upper
                       {'levels': 1},
                       {'usd_per_entry': 0},
                       {'count': 0}]
        for change in bad_changes:
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    check_config([{**good, **change}])

    def test_check_config_rejects_missing_key(self):
        bot = {'exchange': 'kraken', 'pair': 'CRV/USD', 'lower': 0.14, 'upper': 1.14,
               'levels': 71, 'count': 2}               # no usd_per_entry
        with self.assertRaises(ValueError):
            check_config([bot])


    # ---------- open_positions ----------

    def test_open_positions_longs_and_shorts(self):
        trades = [
            {'side': 'long', 'status': 'exit_open', 'amount': 100, 'entry_cost': 30.0},
            {'side': 'long', 'status': 'exit_open', 'amount': 50, 'entry_cost': 16.0},
            {'side': 'short', 'status': 'exit_open', 'amount': 20, 'entry_cost': 8.0},
            {'side': 'long', 'status': 'entry_open', 'amount': 999, 'entry_cost': None},  # not filled: ignored
        ]
        held = open_positions(trades, 0.3)
        self.assertEqual(held['longs'], 2)
        self.assertEqual(held['long_amount'], 150)          # 100 + 50
        self.assertEqual(held['long_cost'], 46.0)           # 30 + 16
        self.assertAlmostEqual(held['long_value'], 45.0)    # 150 * 0.3
        self.assertEqual(held['shorts'], 1)
        self.assertAlmostEqual(held['short_buyback'], 6.0)  # 20 * 0.3
        self.assertAlmostEqual(held['open_pnl'], 1.0)       # (45 - 46) + (8 - 6)

    def test_open_positions_nothing_held(self):
        held = open_positions([], 0.3)
        self.assertEqual(held['open_pnl'], 0)

    def test_open_positions_numbers_as_text(self):
        trades = [{'side': 'long', 'status': 'exit_open', 'amount': '10', 'entry_cost': '3'}]
        self.assertAlmostEqual(open_positions(trades, 0.5)['open_pnl'], 2.0)   # 10*0.5 - 3

    # ---------- funding_needs ----------
    # levels 0.1 .. 0.5, price 0.25, $10 per entry

    def test_funding_needs_no_trades(self):
        needs = funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.25, [], 10)
        self.assertEqual(needs['quote'], 20)                            # longs at 0.1 and 0.2
        self.assertAlmostEqual(needs['coin'], 10/0.3 + 10/0.4 + 10/0.5) # 33.33 + 25 + 20

    def test_funding_needs_held_long(self):
        trades = [{'side': 'long', 'status': 'exit_open', 'entry_price': 0.2,
                   'exit_price': 0.3, 'amount': 50}]
        needs = funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.25, trades, 10)
        self.assertEqual(needs['quote'], 10)                    # only 0.1 is free below
        self.assertAlmostEqual(needs['coin'], 25 + 20 + 50)     # shorts at 0.4, 0.5 + 50 coins in the exit

    def test_funding_needs_held_short(self):
        trades = [{'side': 'short', 'status': 'exit_open', 'entry_price': 0.3,
                   'exit_price': 0.2, 'amount': 40}]
        needs = funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.25, trades, 10)
        self.assertAlmostEqual(needs['quote'], 10 + 8)          # long at 0.1 + buy back 40 @ 0.2
        self.assertAlmostEqual(needs['coin'], 25 + 20)          # shorts at 0.4, 0.5

    def test_funding_needs_skips_bottom_level_for_shorts(self):
        needs = funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.05, [], 10)
        self.assertEqual(needs['quote'], 0)
        self.assertAlmostEqual(needs['coin'], 50 + 10/0.3 + 25 + 20)   # 0.1 can't be a short

    def test_funding_needs_skips_top_level_for_longs(self):
        needs = funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.6, [], 10)
        self.assertEqual(needs['quote'], 40)            # 0.1-0.4; 0.5 has no exit above it
        self.assertEqual(needs['coin'], 0)

    def test_funding_needs_live_entries_do_not_take_levels(self):
        trades = [{'side': 'long', 'status': 'entry_open', 'entry_price': 0.2,
                   'exit_price': 0.3, 'amount': 50}]
        self.assertEqual(funding_needs([0.1, 0.2, 0.3, 0.4, 0.5], 0.25, trades, 10)['quote'], 20)


if __name__ == '__main__':
    unittest.main()


