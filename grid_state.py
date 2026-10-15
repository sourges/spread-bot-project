import json
import os


def save_grid_state(state, filename='grid_state.json'):
    """Save the grid bot's state (levels, trades, profit) to a JSON file.

    Writes to a temporary file first, then swaps it in. If the computer
    crashes halfway through writing, the old state file is still intact
    instead of being left half-written."""
    temp = filename + '.tmp'
    with open(temp, 'w') as f:
        json.dump(state, f, indent=2)
    os.replace(temp, filename)


def load_grid_state(filename='grid_state.json'):
    """Return the saved state dict, or None if there's no state file yet."""
    try:
        with open(filename) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
