"""Zero a grid bot's profit and trade count, e.g. after reinvesting the profit.

    python reset_profit.py kraken CRV/USD

Run it ONLY while the grid program is stopped: a running bot would write the
old numbers back on its next change. Open trades and levels are not touched.
The old totals are written to the bot's grid_trades_....txt first, so the
history is never lost."""
import sys
from datetime import datetime
from grid import bot_name
from grid_state import load_grid_state, save_grid_state


def reset_profit(state_file, log_file, now):
    """Zero total_profit and completed. Returns (old_profit, old_completed).
    Raises FileNotFoundError if the bot has no state file."""
    state = load_grid_state(state_file)
    if state is None:
        raise FileNotFoundError(state_file)
    old_profit = float(state['total_profit'])
    old_completed = state['completed']
    with open(log_file, 'a') as f:
        f.write(f"{now} - PROFIT RESET (reinvested): was ${old_profit:.4f}, "
                f"{old_completed} trades\n")
    state['total_profit'] = 0.0
    state['completed'] = 0
    save_grid_state(state, state_file)
    return old_profit, old_completed


def main():
    if len(sys.argv) != 3:
        print("Usage: python reset_profit.py <exchange> <PAIR>   e.g. kraken CRV/USD")
        return
    name = bot_name(sys.argv[1], sys.argv[2])
    state_file = f"grid_state_{name}.json"
    log_file = f"grid_trades_{name}.txt"

    state = load_grid_state(state_file)
    if state is None:
        print(f"❌ {state_file} not found. Check the exchange and pair spelling.")
        return
    print(f"{sys.argv[1]} {sys.argv[2]}: profit ${float(state['total_profit']):.4f}, "
          f"{state['completed']} trades done, {len(state['trades'])} open trades (kept).")
    answer = input("Is the grid program STOPPED? Type yes to reset: ")
    if answer.lower() != 'yes':
        print("Nothing changed.")
        return
    old_profit, old_completed = reset_profit(state_file, log_file, datetime.now())
    print(f"✅ Reset. Old totals (${old_profit:.4f}, {old_completed} trades) saved in {log_file}.")


if __name__ == '__main__':
    main()
