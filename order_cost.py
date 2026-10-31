import ccxt


def trades_cost(exchange, order, symbol):
    """Exact USD value of what an order filled, from its trades. None if not available.

    Why: Kraken cuts the order's average price off at the pair's decimals
    (USDG: a fill at exactly 0.9999 can come back as average 0.9998), and ccxt
    works out the order's cost as filled x average. For USDG one tick is the
    whole profit, so the profit came out doubled. The trades have the real
    price and cost (the "Trades" tab on Kraken), so add those up instead.

    Only Kraken needs this (Coinbase reports the real filled value).
    Returns None (caller uses the order's own numbers) when:
      - not Kraken, or the order lists no trades
      - the trades don't add up to the filled amount, or a cost is missing
      - Kraken refuses the request for some other reason (printed)
    A network problem is raised, so the caller tries again next cycle
    instead of recording a wrong profit."""
    trade_ids = order.get('trades') or []
    if exchange.id != 'kraken' or not trade_ids:
        return None
    try:
        trades = exchange.fetch_order_trades(order['id'], symbol, params={'trades': trade_ids})
    except ccxt.NetworkError:
        raise
    except Exception as e:
        print(f"Could not read trades for order {order.get('id')} ({e}), using the order's average.")
        return None
    if not trades or any(t.get('cost') is None or t.get('amount') is None for t in trades):
        return None
    amount = sum(float(t['amount']) for t in trades)
    filled = float(order['filled'])
    if abs(amount - filled) > filled * 1e-9:
        return None                     # some trades missing: don't trust a partial sum
    return sum(float(t['cost']) for t in trades)
