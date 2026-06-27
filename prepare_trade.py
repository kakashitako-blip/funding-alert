#!/usr/bin/env python3
"""
Semi-auto trade prep — builds a SHORT order ticket (entry/SL/TP sized to your
risk) from live data. DRY RUN by default; only places the order with --execute
AND a typed confirmation. Run on your Mac (Bybit/Binance geo-blocked from cloud).

  python prepare_trade.py ARK                          # dry run, suggested levels
  python prepare_trade.py ARK --entry 0.140 --tp 0.103 # dry run, your levels
  python prepare_trade.py ARK --entry 0.140 --execute  # place it (asks to confirm)

Execution needs ccxt + env vars BYBIT_API_KEY / BYBIT_API_SECRET (trade perm only,
NO withdrawal, IP-whitelisted). Add --testnet to dry-fire on Bybit testnet first.
"""
import argparse, sys, statistics, requests

MAX_RISK = 300.0          # hard cap — refuses bigger without editing this line
DEFAULT_SL_PCT = 20.0
DEFAULT_LEVERAGE = 4      # liq (~-25%) sits beyond the -20% stop = buffer

def bybit_price(coin):
    j = requests.get("https://api.bybit.com/v5/market/tickers",
                     params={"category": "linear", "symbol": f"{coin}USDT"}, timeout=8).json()
    return float(j["result"]["list"][0]["lastPrice"])

def pre_pump_base(coin):
    """15th percentile of lows over 14d (4h candles) — the liquidity shelf / TP."""
    j = requests.get("https://api.bybit.com/v5/market/kline",
                     params={"category": "linear", "symbol": f"{coin}USDT", "interval": "240", "limit": 84},
                     timeout=8).json()
    lows = sorted(float(c[3]) for c in j["result"]["list"])
    return lows[int(len(lows) * 0.15)] if lows else None

def premium_snapshot(coin):
    try:
        import premium as P
        out = []
        for nm, fn in [("Binance", P.binance), ("Bybit", P.bybit), ("MEXC", P.mexc)]:
            try: out.append((nm, fn(coin)["premium"], fn(coin)["funding"]))
            except Exception: pass
        return sorted(out, key=lambda x: x[1])
    except Exception:
        return []

def build(coin, entry, sl_pct, tp, risk, leverage):
    sl = entry * (1 + sl_pct / 100)
    reward_pct = (entry - tp) / entry * 100
    notional = risk / (sl_pct / 100)
    qty = notional / entry
    margin = notional / leverage
    rr = reward_pct / sl_pct
    return {"side": "SHORT", "entry": entry, "sl": sl, "sl_pct": sl_pct, "tp": tp,
            "reward_pct": reward_pct, "risk": risk, "notional": notional, "qty": qty,
            "margin": margin, "leverage": leverage, "rr": rr}

def ticket(coin, o, prem):
    print(f"\n{'='*46}\n  TRADE TICKET — {coin} ({o['side']})\n{'='*46}")
    print(f"  Side        SHORT (limit)")
    print(f"  Entry       {o['entry']:.6g}")
    print(f"  Stop loss   {o['sl']:.6g}   (+{o['sl_pct']:.0f}%)")
    print(f"  Take profit {o['tp']:.6g}   (-{o['reward_pct']:.1f}%)")
    print(f"  R:R         {o['rr']:.2f}   {'✅' if o['rr']>=1 else '⚠️ below 1:1'}")
    print(f"  {'-'*42}")
    print(f"  Risk        ${o['risk']:.0f}  (if stop hit)")
    print(f"  Position    ${o['notional']:.0f} notional  =  {o['qty']:.4g} {coin}")
    print(f"  Margin      ${o['margin']:.0f}  at {o['leverage']}x  (liq ~-{100/o['leverage']:.0f}%, beyond stop)")
    if prem:
        worst = prem[0]
        line = " | ".join(f"{n} {p:+.1f}%" for n, p, f in prem)
        print(f"  {'-'*42}")
        print(f"  Premium     {line}")
        print(f"  Most selling: {worst[0]} ({worst[1]:+.2f}%)")
    print(f"{'='*46}")

def execute(coin, o, testnet):
    try:
        import ccxt, os
    except ImportError:
        print("\n✗ ccxt not installed.  pip install ccxt"); return
    import os
    key, sec = os.environ.get("BYBIT_API_KEY"), os.environ.get("BYBIT_API_SECRET")
    if not key or not sec:
        print("\n✗ Set BYBIT_API_KEY and BYBIT_API_SECRET env vars (trade perm, no withdrawal)."); return
    if o["risk"] > MAX_RISK:
        print(f"\n✗ Risk ${o['risk']:.0f} exceeds hard cap ${MAX_RISK:.0f}. Edit MAX_RISK to override."); return

    print(f"\n⚠️  ABOUT TO PLACE A {'TESTNET' if testnet else 'LIVE'} ORDER:")
    print(f"   SHORT {o['qty']:.4g} {coin} @ {o['entry']:.6g} limit | SL {o['sl']:.6g} | TP {o['tp']:.6g}")
    if input('   Type "CONFIRM" to place, anything else to abort: ').strip() != "CONFIRM":
        print("   Aborted — no order placed."); return

    ex = ccxt.bybit({"apiKey": key, "secret": sec, "options": {"defaultType": "linear"}})
    if testnet: ex.set_sandbox_mode(True)
    sym = f"{coin}/USDT:USDT"
    try:
        ex.set_leverage(o["leverage"], sym)
    except Exception as e:
        print(f"   (leverage set skipped: {str(e)[:60]})")
    order = ex.create_order(sym, "limit", "sell", o["qty"], o["entry"], params={
        "stopLoss": {"triggerPrice": o["sl"]},
        "takeProfit": {"triggerPrice": o["tp"]},
        "positionIdx": 0,
    })
    print(f"\n✅ Order placed: id {order.get('id')}  status {order.get('status')}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("coin")
    ap.add_argument("--entry", type=float, help="limit entry (default: current price)")
    ap.add_argument("--tp", type=float, help="take profit (default: pre-pump base)")
    ap.add_argument("--sl-pct", type=float, default=DEFAULT_SL_PCT)
    ap.add_argument("--risk", type=float, default=100.0)
    ap.add_argument("--leverage", type=int, default=DEFAULT_LEVERAGE)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--testnet", action="store_true")
    a = ap.parse_args()
    coin = a.coin.upper().replace("USDT", "")

    cur = bybit_price(coin)
    entry = a.entry if a.entry else cur
    tp = a.tp if a.tp else pre_pump_base(coin)
    prem = premium_snapshot(coin)

    print(f"\n{coin}: live {cur:.6g}" + (f"   (entry {entry:.6g})" if a.entry else "   (entry = current)"))
    if not tp:
        print("✗ couldn't compute TP — pass --tp"); return
    o = build(coin, entry, a.sl_pct, tp, a.risk, a.leverage)
    ticket(coin, o, prem)

    if a.execute:
        execute(coin, o, a.testnet)
    else:
        flags = f"--entry {entry:.6g} --tp {tp:.6g}"
        print(f"\n[DRY RUN] to place:  python prepare_trade.py {coin} {flags} --execute")
        print(f"          test first:  ... --execute --testnet\n")

if __name__ == "__main__":
    main()
