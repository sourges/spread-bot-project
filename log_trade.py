
from datetime import datetime
filename = 'trades.txt' 

def log_trade(side, price, amount, order_id):
    now = datetime.now()
    trade = f"{now} - {side}, {price}, {amount}, {order_id}\n"
    with open(filename, 'a') as f:
        f.write(trade)
