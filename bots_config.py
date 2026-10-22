# Grid bot settings. One dict per bot. To add a bot, add a dict and restart.
#
# exchange:      'kraken' (Coinbase coming)
# pair:          as the exchange names it, e.g. 'CRV/USD'
# lower, upper:  grid range
# levels:        number of grid prices (levels - 1 gaps; 71 levels here ≈ 3.02% apart)
# usd_per_entry: dollars per entry order
# count:         live entries per side (2 = 2 buys below + 2 sells above)
#
# Never change lower/upper/levels on a bot with open trades: the saved
# levels win anyway, and you'll get a warning.

BOTS = [
    {'exchange': 'kraken', 'pair': 'CRV/USD',
     'lower': 0.1431, 'upper': 1.1442, 'levels': 71,
     'usd_per_entry': 61, 'count': 2},

    {'exchange': 'coinbase', 'pair': 'CRV/USDC',
    'lower': 0.2, 'upper': 1.2, 'levels': 61,
    'usd_per_entry': 60, 'count': 2},
]



CHECK_INTERVAL = 60     # seconds between cycles, for every bot

# DRY_RUN = True: connect, show each bot's plan, place NOTHING.
DRY_RUN = False
