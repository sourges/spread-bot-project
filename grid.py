def grid_levels(lower, upper, num_levels):
    """ Return num_levels evenly spaced prices from lower to upper, rounded to 4 decimalss """
    if num_levels < 2:
        raise ValueError("Grid needs at least 2 levels.")
    distance = upper - lower
    step = distance / (num_levels - 1)
    levels = [round(lower + i * step, 4) for i in range(num_levels)]
    return levels


         

answer = grid_levels(0.9990, 1.0010, 5)
print(answer)