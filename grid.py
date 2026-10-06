import math

def grid_levels(lower, upper, num_levels, decimals=4):
    """ Return num_levels evenly spaced prices from lower to upper, rounded to 4 decimals """
    if num_levels < 2:
        raise ValueError("Grid needs at least 2 levels.")
    distance = upper - lower
    step = distance / (num_levels - 1)
    levels = [round(lower + i * step, decimals) for i in range(num_levels)]
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

def centered_grid(current_price, step_percent, levels_per_side, decimals=4):
    current_price = round(current_price, decimals)
    step = current_price * step_percent / 100
    lower = current_price - (step * levels_per_side)
    upper = current_price + (step * levels_per_side)
    total_levels = levels_per_side * 2 + 1
    levels = grid_levels(lower, upper, total_levels, decimals = decimals)
    return levels

def tick_to_decimals(tick):
    return round(-math.log10(tick))

def geometric_levels(lower, upper, num_levels, decimals=4):
    """ return geometric levels """
    if num_levels < 2:
        raise ValueError("Grid needs at least 2 levels.")
    ratio = (upper / lower) ** (1 / (num_levels - 1))
    levels = [round(lower * ratio ** i, decimals) for i in range(num_levels)]
    return levels

def taken_levels(trades):
    """ Return prices used by trades whose entry has already filled """
    taken = []
    for trade in trades:
        if trade['status'] == 'entry_open':
            continue                        # a live, unfilled entry doesn't block its own level
        taken.append(trade['entry_price'])
        taken.append(trade['exit_price'])

    return taken

def free_levels_below(levels, current_price, trades, count=2):
    """Return up to `count` grid levels below current_price where buy
    orders should be, nearest first. Levels used by trades whose entry
    has already filled are skipped."""

    taken = taken_levels(trades)
    
    # walk the grid from the top down, keeping free levels below the price
    free = []
    for level in reversed(levels):
        if level >= current_price:
            continue                      # at or above the price, not a buy level
        if level in taken:
            continue                      # in use by a filled trade
        free.append(level)
        if len(free) == count:
            return free                   # found enough, stop early

    return free                           # fewer than `count` found (bottom of grid)

def free_levels_above(levels, current_price, trades, count=2):
    """Return up to `count` grid levels above current_price where sell
    orders should be, nearest first. Levels used by trades whose entry
    has already filled are skipped."""

    taken = taken_levels(trades)

    # walk the grid from the bottom up, keeping free levels above the price
    free = []
    for level in levels:
        if level <= current_price:
            continue                      # at or below the price, not a sell level
        if level in taken:
            continue                      # in use by a filled trade
        free.append(level)
        if len(free) == count:
            return free  

    return free  

# def free_levels_below(levels, current_price, trades, count=2):
#     taken = []
#     for trade in trades:
#         taken.append(trade['entry_price'])
#         taken.append(trade['exit_price'])
#     free = []
#     for level in reversed(levels):
#         if level >= current_price:
#             continue
#         if level in taken:
#             continue
#         free.append(level)
#         if len(free) == count:
#             return free
#     return free


if __name__ == '__main__':
    current_price = round(1, 4)
    levels = centered_grid(current_price, 0.02, 10)
    answer = split_levels(levels, current_price)
    print(answer)

