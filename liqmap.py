#!/usr/bin/env python3
"""
DIY liquidation map — approximates the Coinglass heatmap without screenshots.
Logic: positions get opened where volume trades. Project where longs/shorts at
common leverage tiers (25/50/100x) would liquidate, weight by that candle's
turnover, bin, and surface the biggest clusters.

  Short liqs ABOVE price = upside sweep magnets (squeeze targets)
  Long liqs BELOW price  = downside cascade fuel (where a dump accelerates)

  python3 liqmap.py POWR
"""
import argparse, requests

TIERS = [25, 50, 100]   # leverage tiers that form the visible clusters

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("coin")
    ap.add_argument("--interval", default="15"); ap.add_argument("--limit", type=int, default=200)
    a = ap.parse_args()
    coin = a.coin.upper().replace("USDT", "")
    sym = coin + "USDT"

    cur = float(requests.get("https://api.bybit.com/v5/market/tickers",
                params={"category": "linear", "symbol": sym}, timeout=8).json()["result"]["list"][0]["lastPrice"])
    k = requests.get("https://api.bybit.com/v5/market/kline",
                params={"category": "linear", "symbol": sym, "interval": a.interval, "limit": a.limit},
                timeout=10).json()["result"]["list"]
    # [start, o, h, l, c, vol, turnover]
    candles = [{"p": (float(c[2]) + float(c[3]) + float(c[4])) / 3, "w": float(c[6])} for c in k]

    up, down = {}, {}     # 1%-bin (relative to current) -> aggregated turnover weight
    for c in candles:
        for lev in TIERS:
            sl = c["p"] * (1 + 1 / lev)          # short liquidation (above)
            ll = c["p"] * (1 - 1 / lev)          # long liquidation (below)
            bs = round((sl - cur) / cur * 100)
            bl = round((ll - cur) / cur * 100)
            if bs > 0:
                up[bs] = up.get(bs, 0) + c["w"]
            if bl < 0:
                down[bl] = down.get(bl, 0) + c["w"]

    mx = max(list(up.values()) + list(down.values()) + [1])
    def show(d, label, reverse):
        rows = sorted(d.items(), key=lambda x: -x[1])[:5]
        rows.sort(key=lambda x: x[0], reverse=reverse)
        print(f"  {label}")
        for pct, w in rows:
            price = cur * (1 + pct / 100)
            bar = "█" * max(1, round(w / mx * 18))
            print(f"    {price:.6g}  ({pct:+d}%)  {bar}")

    print(f"\n{coin}  liquidation map  (current {cur:.6g})\n")
    show(up, "↑ SHORT liqs above  (upside sweep magnets)", True)
    print()
    show(down, "↓ LONG liqs below  (downside cascade fuel)", True)
    print()

if __name__ == "__main__":
    main()
