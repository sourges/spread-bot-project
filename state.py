import json

def save_state(waiting_for, order_id, filename = 'state.json'):
    trade = {}
    trade['waiting_for'] = waiting_for
    trade['order_id'] = order_id
    with open(filename, 'w') as f:
        json.dump(trade, f)

def load_state(filename = 'state.json'):
    try:
        with open(filename) as f:
            answer = json.load(f)
            return answer
    except FileNotFoundError:
        return None


def save_totals(total_profit, total_trades, filename='spread_totals.json'):
    """Profit and trade count, kept in their own file so they survive restarts."""
    with open(filename, 'w') as f:
        json.dump({'total_profit': total_profit, 'total_trades': total_trades}, f)


def load_totals(filename='spread_totals.json'):
    """Return (total_profit, total_trades), or (0.0, 0) if nothing saved yet."""
    try:
        with open(filename) as f:
            totals = json.load(f)
            return totals['total_profit'], totals['total_trades']
    except FileNotFoundError:
        return 0.0, 0
