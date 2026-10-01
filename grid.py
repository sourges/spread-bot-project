def grid_levels(lower, upper, num_levels):
    """ Return num_levels evenly spaced prices from lower to upper, rounded to 4 decimalss """
    if num_levels < 2:
        raise ValueError("Grid needs at least 2 levels.")
    distance = upper - lower
    step = distance / (num_levels - 1)
    levels = [round(lower + i * step, 4) for i in range(num_levels)]
    return levels

def split_levels(levels, current_price):
    buys = []
    sells = []
    for level in levels:
        if level < current_price:
            buys.append(level)
        elif level > current_price:
            sells.append(level)
    return {'buys': buys, 'sells': sells}
         

levels = grid_levels(0.9990, 1.0010, 5)
orders = split_levels(levels, 0.99)
print(levels)
print(orders)