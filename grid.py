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

def exit_order(trade, filled_amount):
    """Return the exit order (side, price, amount) for a trade whose entry filled."""
    if trade['side'] == 'long':
        return {'side': 'sell', 'price': trade['exit_price'], 'amount': filled_amount}
    elif trade['side'] == 'short':
        return {'side': 'buy', 'price': trade['exit_price'], 'amount': filled_amount}
    else:
        raise ValueError(f"Unknown trade side: {trade['side']}")

def neighbor_level(levels, level, direction):
    """ when bot places a trade, it has to know where the exit goes """
    i = levels.index(level)
    if direction == 'up':
        if i == len(levels) - 1:
            return None
        else:
            return levels[i + 1]
    elif direction == 'down':
        if i == 0:
            return None
        else:
            return levels[i - 1]
    else:
        raise ValueError(f"Unknown trade direction {direction}.")

def live_entries(trades, side):
    """Return live (unfilled) entry trades for one side."""
    result = []
    for trade in trades:
        if trade['status'] == 'entry_open' and trade['side'] == side:
            result.append(trade)
    return result

def plan_entries(levels, current_price, trades, count=2):
    long_targets = free_levels_below(levels, current_price, trades, count)
    short_targets = free_levels_above(levels, current_price, trades, count)
    live_longs = live_entries(trades, 'long')
    live_shorts = live_entries(trades, 'short')
    
    cancel_longs = [t for t in live_longs if t['entry_price'] not in long_targets]
    cancel_shorts = [t for t in live_shorts if t['entry_price'] not in short_targets]
    
    place = []
    live_long_prices = [t['entry_price'] for t in live_longs]
    for target in long_targets:
        if target in live_long_prices:
            continue
        exit_price = neighbor_level(levels, target, 'up')
        if exit_price is None:
            continue
        trade = {'side': 'long', 'entry_price': target, 'exit_price': exit_price,
                'amount': None, 'entry_id': None, 'exit_id': None,
                'status': 'entry_open'}
        place.append(trade)

    live_short_prices = [t['entry_price'] for t in live_shorts]
    for target in short_targets:
        if target in live_short_prices:
            continue
        exit_price = neighbor_level(levels, target, 'down')
        if exit_price is None:
            continue
        trade = {'side': 'short', 'entry_price': target, 'exit_price': exit_price,
                'amount': None, 'entry_id': None, 'exit_id': None,
                'status': 'entry_open'}
        place.append(trade)

    return {'place': place, 'cancel': cancel_longs + cancel_shorts}
    

def order_amount(usd, price, decimals, min_amount):
    """How many coins to order for `usd` dollars at `price`.
    Rounds DOWN to Kraken's amount precision so we never spend more than `usd`.
    Raises ValueError if the result is below Kraken's minimum order size."""
    factor = 10 ** decimals
    amount = math.floor(usd / price * factor) / factor
    if amount < min_amount:
        raise ValueError(f"Amount {amount} is below the minimum {min_amount}.")
    return amount


def bot_name(exchange_id, pair):
    """A name for one bot that is safe to use in file names.
    ('kraken', 'CRV/USD') -> 'kraken_CRV_USD'. The exchange is part of the name
    because the same pair can run on two exchanges."""
    return f"{exchange_id}_{pair.replace('/', '_')}"


def check_config(bots):
    """Catch mistakes in bots_config.BOTS before anything trades.
    Raises ValueError explaining the first problem found."""
    needed = ['exchange', 'pair', 'lower', 'upper', 'levels', 'usd_per_entry', 'count']
    seen = set()
    for bot in bots:
        for key in needed:
            if key not in bot:
                raise ValueError(f"{bot.get('pair', '?')}: missing '{key}'")
        name = bot_name(bot['exchange'], bot['pair'])
        if name in seen:
            # Two bots would share one state file and fight over it.
            raise ValueError(f"{name} is listed twice")
        seen.add(name)
        if bot['lower'] >= bot['upper']:
            raise ValueError(f"{name}: lower must be below upper")
        if bot['levels'] < 2:
            raise ValueError(f"{name}: needs at least 2 levels")
        if bot['usd_per_entry'] <= 0 or bot['count'] < 1:
            raise ValueError(f"{name}: usd_per_entry and count must be positive")


def open_positions(trades, price):
    """What the filled trades hold right now (before exit fees).

    long:  we bought coins and wait to sell them. Worth amount * price now.
    short: we sold coins and wait to buy them back. Costs amount * price now.
    open_pnl: what we'd make (or lose) if every open trade closed at `price`."""
    longs = [t for t in trades if t['status'] == 'exit_open' and t['side'] == 'long']
    shorts = [t for t in trades if t['status'] == 'exit_open' and t['side'] == 'short']
    # float(): old saved trades may hold numbers as text
    long_amount = sum(float(t['amount']) for t in longs)
    long_cost = sum(float(t['entry_cost']) for t in longs)
    short_amount = sum(float(t['amount']) for t in shorts)
    short_proceeds = sum(float(t['entry_cost']) for t in shorts)
    long_value = long_amount * price
    short_buyback = short_amount * price
    return {
        'longs': len(longs), 'long_amount': long_amount,
        'long_cost': long_cost, 'long_value': long_value,
        'shorts': len(shorts), 'short_amount': short_amount,
        'short_proceeds': short_proceeds, 'short_buyback': short_buyback,
        'open_pnl': (long_value - long_cost) + (short_proceeds - short_buyback),
    }


def funding_needs(levels, price, trades, usd_per_entry):
    """Money this bot needs on the exchange to cover its WHOLE range.

    quote: USD for a long entry at every free level below the price,
           plus USD locked in open short exits (buy orders).
    coin:  coins for a short entry at every free level above the price,
           plus coins locked in open long exits (sell orders).
    Levels used by filled trades are skipped (their money is in the exits).
    The top level can't be a long (no exit above it); the bottom can't be a short."""
    taken = taken_levels(trades)
    quote = 0.0
    coin = 0.0
    for i, level in enumerate(levels):
        if level in taken:
            continue
        if level < price and i < len(levels) - 1:
            quote += usd_per_entry
        elif level > price and i > 0:
            coin += usd_per_entry / level
    for t in trades:
        if t['status'] != 'exit_open':
            continue
        if t['side'] == 'long':
            coin += float(t['amount'])
        else:
            quote += float(t['amount']) * float(t['exit_price'])
    return {'quote': quote, 'coin': coin}


def range_position(levels, price):
    """'below' under the lowest level, 'above' over the highest, else 'inside'."""
    if price < levels[0]:
        return 'below'
    if price > levels[-1]:
        return 'above'
    return 'inside'


if __name__ == '__main__':
    levels = [0.32829, 0.33818, 0.34835, 0.35884, 0.36964, 0.38077]
    current_price = 0.3795
    trades = [
        {'side': 'long', 'entry_price': 0.35884, 'exit_price': 0.36964,
         'amount': None, 'entry_id': 'A', 'exit_id': None,
         'status': 'entry_open'},
        {'side': 'long', 'entry_price': 0.34835, 'exit_price': 0.35884,
         'amount': None, 'entry_id': 'B', 'exit_id': None,
         'status': 'entry_open'},
    ]
    plan = plan_entries(levels, current_price, trades)
    print(plan)
    

