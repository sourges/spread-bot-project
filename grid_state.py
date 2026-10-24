import json
import os
import time


def save_grid_state(state, filename='grid_state.json'):
    """Save the grid bot's state (levels, trades, profit) to a JSON file.

    Writes to a temporary file first, then swaps it in. If the computer
    crashes halfway through writing, the old state file is still intact
    instead of being left half-written."""
    temp = filename + '.tmp'
    with open(temp, 'w') as f:
        json.dump(state, f, indent=2)
    replace_with_retries(temp, filename)


def replace_with_retries(temp, filename, attempts=5, wait=0.2):
    """os.replace, but Windows sometimes refuses for a moment ("Access is denied")
    because another program (antivirus, OneDrive, search) has the file open.
    Wait a little and try again instead of stopping the bot."""
    for attempt in range(1, attempts + 1):
        try:
            os.replace(temp, filename)
            return
        except PermissionError:
            if attempt == attempts:
                raise               # still locked after ~1 second: a real problem
            time.sleep(wait)


def load_grid_state(filename='grid_state.json'):
    """Return the saved state dict, or None if there's no state file yet."""
    try:
        with open(filename) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
