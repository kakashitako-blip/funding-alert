#!/usr/bin/env python3
"""
Local 3-CEX premium reader — shows real-time premium index (perp-vs-spot pressure)
across Binance, Bybit, MEXC for a coin. Run on your Mac (Binance/Bybit are
geo-blocked from the cloud bot).

Usage:  python premium.py ARK
"""
import requests, sys, statistics

def binance(coin):
    sym = f"{coin}USDT"
    r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": sym}, timeout=8).json()
    mark, idx = float(r["markPrice"]), float(r["indexPrice"])
    prem = (mark - idx) / idx * 100
    fund = float(r["lastFundingRate"]) * 100
    # recent 5m premium trend (last 6 = 30min)
    pk = requests.get("https://fapi.binance.com/fapi/v1/premiumIndexKlines",
                      params={"symbol": sym, "interval": "5m", "limit": 6}, timeout=8).json()
    trend = [float(c[4]) * 100 for c in pk]
    return {"premium": prem, "funding": fund, "trend": trend}

def bybit(coin):
    sym = f"{coin}USDT"
    pk = requests.get("https://api.bybit.com/v5/market/premium-index-price-kline",
                      params={"category": "linear", "symbol": sym, "interval": "5", "limit": 6}, timeout=8).json()
    lst = pk["result"]["list"]            # newest first
    trend = [float(c[4]) * 100 for c in reversed(lst)]
    prem = trend[-1]
    tk = requests.get("https://api.bybit.com/v5/market/tickers",
                      params={"category": "linear", "symbol": sym}, timeout=8).json()["result"]["list"][0]
    return {"premium": prem, "funding": float(tk["fundingRate"]) * 100, "trend": trend}

def mexc(coin):
    sym = f"{coin}_USDT"
    t = requests.get("https://contract.mexc.com/api/v1/contract/ticker", params={"symbol": sym}, timeout=8).json()["data"]
    fair, idx = float(t["fairPrice"]), float(t["indexPrice"])
    prem = (fair - idx) / idx * 100
    return {"premium": prem, "funding": float(t["fundingRate"]) * 100, "trend": None}

def spark(vals):
    if not vals: return ""
    blocks = "▁▂▃▄▅▆▇█"
    lo, hi = min(vals), max(vals)
    if hi == lo: return blocks[0] * len(vals)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in vals)

def main():
    coin = (sys.argv[1] if len(sys.argv) > 1 else "ARK").upper().replace("USDT", "")
    print(f"\n{coin} — real-time premium index (perp below spot = sellers paying)\n")
    rows = []
    for name, fn in [("Binance", binance), ("Bybit", bybit), ("MEXC", mexc)]:
        try:
            d = fn(coin)
            rows.append((name, d))
        except Exception as e:
            print(f"  {name:<8} — n/a ({str(e)[:40]})")
    rows.sort(key=lambda x: x[1]["premium"])   # most negative first
    print(f"  {'Venue':<8}{'Premium':>10}{'Funding':>10}   {'30m trend (5m)':<18}")
    print("  " + "-" * 50)
    for i, (name, d) in enumerate(rows):
        tag = "  <-- most selling" if i == 0 and d["premium"] < 0 else ""
        tr = f"{spark(d['trend'])} {d['trend'][-1]:+.2f}%" if d["trend"] else ""
        print(f"  {name:<8}{d['premium']:>9.3f}%{d['funding']:>9.3f}%   {tr}{tag}")
    if rows:
        worst = rows[0]
        print(f"\n  Strongest sell pressure: {worst[0]} (premium {worst[1]['premium']:+.2f}%)")
        avg = statistics.mean(r[1]["premium"] for r in rows)
        print(f"  Avg premium across {len(rows)} CEXs: {avg:+.2f}%")
    print()

if __name__ == "__main__":
    main()
