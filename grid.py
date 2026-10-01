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

def centered_grid(current_price, step, levels_per_side):
    current_price = round(current_price, 4)
    lower = current_price - (step * levels_per_side)
    upper = current_price + (step * levels_per_side)
    total_levels = levels_per_side * 2 + 1
    levels = grid_levels(lower, upper, total_levels)
    return levels
         
if __name__ == '__main__':
    current_price = round(1, 4)
    levels = centered_grid(current_price, 0.0002, 10)
    answer = split_levels(levels, current_price)
    print(answer)

