import ccxt 
from grid import centered_grid, split_levels, tick_to_decimals

exchange = ccxt.kraken()
markets = exchange.load_markets()
symbol = 'CAKE/USD'

market = markets[symbol]
tick = market['precision']['price']
minimum = market['limits']['amount']['min']


ticker = exchange.fetch_ticker(symbol)
current_price = ticker['last']
decimals = tick_to_decimals(tick)
current_price = round(current_price, decimals)
grid = centered_grid(current_price, 0.5, 10, decimals)
split_grid = split_levels(grid, current_price)


print(split_grid)
print(minimum)
