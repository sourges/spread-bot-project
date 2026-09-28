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

# {"waiting_for": "sell", "order_id": "O6YHBH-4ATPI-7GSK2N"}
#save_state("sell", "O6YHBH-4ATPI-7GSK2N")
answer = load_state()
