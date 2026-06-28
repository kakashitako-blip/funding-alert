#!/usr/bin/env python3
"""
Live order-book walls across Binance / Bybit / MEXC for a coin.
Shows the biggest resting bid walls (support below) and ask walls (resistance
above), with % distance from mid and $ notional. Run locally.

  python3 orderbook.py POWR
"""
import argparse, requests

def walls(asks, bids, mid, n=3):
    a = sorted(asks, key=lambda x: -x[1] * x[0])[:n]   # by $ size
    b = sorted(bids, key=lambda x: -x[1] * x[0])[:n]
    a.sort(key=lambda x: x[0])
    b.sort(key=lambda x: -x[0])
    return a, b

def fmt_side(rows, mid, sign):
    out = []
    for p, q in rows:
        pct = (p - mid) / mid * 100
        out.append(f"      {p:.6g}  ({pct:+.1f}%)  ${p*q/1000:.0f}k")
    return out

def binance(coin):
    d = requests.get("https://fapi.binance.com/fapi/v1/depth",
                     params={"symbol": f"{coin}USDT", "limit": 500}, timeout=8).json()
    asks = [[float(p), float(q)] for p, q in d["asks"]]
    bids = [[float(p), float(q)] for p, q in d["bids"]]
    return asks, bids

def bybit(coin):
    d = requests.get("https://api.bybit.com/v5/market/orderbook",
                     params={"category": "linear", "symbol": f"{coin}USDT", "limit": 200}, timeout=8).json()["result"]
    asks = [[float(p), float(q)] for p, q in d["a"]]
    bids = [[float(p), float(q)] for p, q in d["b"]]
    return asks, bids

def mexc(coin):
    d = requests.get(f"https://contract.mexc.com/api/v1/contract/depth/{coin}_USDT",
                     params={"limit": 200}, timeout=8).json()["data"]
    asks = [[a[0], a[1]] for a in d["asks"]]
    bids = [[b[0], b[1]] for b in d["bids"]]
    return asks, bids

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coin")
    a = ap.parse_args()
    coin = a.coin.upper().replace("USDT", "")
    print(f"\n{coin} — order-book walls + flow (biggest resting orders)\n")
    tot_bid = tot_ask = 0.0
    for name, fn in [("Binance", binance), ("Bybit", bybit), ("MEXC", mexc)]:
        try:
            asks, bids = fn(coin)
            mid = (asks[0][0] + bids[0][0]) / 2
            aw, bw = walls(asks, bids, mid)
            # ±1% liquidity (the "delta"/pressure: ask-heavy = sellers stacked = bearish)
            bid1 = sum(p * q for p, q in bids if p >= mid * 0.99)
            ask1 = sum(p * q for p, q in asks if p <= mid * 1.01)
            tot_bid += bid1; tot_ask += ask1
            imb = (bid1 - ask1) / (bid1 + ask1) * 100 if (bid1 + ask1) else 0
            print(f"  {name}  (mid {mid:.6g})  ±1%: bid ${bid1/1000:.0f}k / ask ${ask1/1000:.0f}k  -> {imb:+.0f}% {'(buyers)' if imb>0 else '(sellers)'}")
            print(f"    resistance (ask walls):")
            for line in fmt_side(aw, mid, 1):
                print(line)
            print(f"    support (bid walls):")
            for line in fmt_side(bw, mid, -1):
                print(line)
            print()
        except Exception as e:
            print(f"  {name}: n/a ({str(e)[:40]})\n")
    if tot_bid + tot_ask:
        net = (tot_bid - tot_ask) / (tot_bid + tot_ask) * 100
        side = "BUYERS stacked (support)" if net > 0 else "SELLERS stacked (distribution)"
        print(f"  AGGREGATE ±1%:  bid ${tot_bid/1000:.0f}k / ask ${tot_ask/1000:.0f}k  ->  {net:+.0f}%  {side}\n")

if __name__ == "__main__":
    main()
