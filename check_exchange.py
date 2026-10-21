"""Read-only check of an exchange and pair before giving a bot real money.
Places nothing, cancels nothing.

    python check_exchange.py coinbase CRV/USDC
"""
import os
import sys
import ccxt
from dotenv import load_dotenv


def main(name, pair):
    load_dotenv()
    key = os.getenv(f'{name.upper()}_API_KEY')
    secret = os.getenv(f'{name.upper()}_API_SECRET')
    if not key or not secret:
        print(f"❌ Missing {name.upper()}_API_KEY or {name.upper()}_API_SECRET in .env")
        return
    exchange = getattr(ccxt, name)({'apiKey': key, 'secret': secret})

    exchange.load_markets()
    if pair not in exchange.markets:
        close = [s for s in exchange.markets if s.startswith(pair.split('/')[0] + '/')]
        print(f"❌ {pair} not found. Pairs for that coin: {close}")
        return
    market = exchange.market(pair)
    print(f"Market {pair}: price tick {market['precision']['price']} | "
          f"amount step {market['precision']['amount']} | "
          f"min amount {market['limits']['amount']['min']} | "
          f"min cost {market['limits']['cost']['min']}")

    print(f"Price: {exchange.fetch_ticker(pair)['last']}")

    # Private calls from here on: these prove the key works.
    coin, quote = pair.split('/')
    total = exchange.fetch_balance()['total']
    print(f"Balance: {total.get(coin, 0)} {coin} | {total.get(quote, 0)} {quote}")

    orders = exchange.fetch_open_orders(pair)
    print(f"Open orders on {pair}: {len(orders)}")
    for o in orders:
        print(f"  {o['side']} {o['amount']} @ {o['price']}")

    if exchange.has.get('fetchTradingFee'):
        fee = exchange.fetch_trading_fee(pair)
        print(f"Your fees: maker {fee['maker']} | taker {fee['taker']}")

    print("✅ Key works. Nothing was placed or cancelled.")


if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Usage: python check_exchange.py coinbase CRV/USDC")
    else:
        main(sys.argv[1], sys.argv[2])
