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

def _api(testnet):
    return "https://api-testnet.bybit.com" if testnet else "https://api.bybit.com"

def bybit_price(coin, testnet=False):
    j = requests.get(f"{_api(testnet)}/v5/market/tickers",
                     params={"category": "linear", "symbol": f"{coin}USDT"}, timeout=8).json()
    return float(j["result"]["list"][0]["lastPrice"])

def pre_pump_base(coin, testnet=False):
    """15th percentile of lows over 14d (4h candles) — the liquidity shelf / TP."""
    j = requests.get(f"{_api(testnet)}/v5/market/kline",
                     params={"category": "linear", "symbol": f"{coin}USDT", "interval": "240", "limit": 84},
                     timeout=8).json()
    rows = j.get("result", {}).get("list", [])
    lows = sorted(float(c[3]) for c in rows)
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

def execute(coin, o, testnet, market=False):
    try:
        import ccxt
    except ImportError:
        print("\n✗ ccxt not installed.  pip3 install --break-system-packages ccxt"); return
    import os
    key, sec = os.environ.get("BYBIT_API_KEY"), os.environ.get("BYBIT_API_SECRET")
    if not key or not sec:
        print("\n✗ Set BYBIT_API_KEY and BYBIT_API_SECRET env vars (trade perm, no withdrawal)."); return
    if o["risk"] > MAX_RISK:
        print(f"\n✗ Risk ${o['risk']:.0f} exceeds hard cap ${MAX_RISK:.0f}. Edit MAX_RISK to override."); return

    ex = ccxt.bybit({"apiKey": key, "secret": sec, "options": {"defaultType": "linear"}})
    if testnet:
        ex.set_sandbox_mode(True)
    sym = f"{coin}/USDT:USDT"
    ex.load_markets()
    # round to the exchange's precision so Bybit doesn't reject
    qty = ex.amount_to_precision(sym, o["qty"])
    price = ex.price_to_precision(sym, o["entry"])
    sl = ex.price_to_precision(sym, o["sl"])
    tp = ex.price_to_precision(sym, o["tp"])

    kind = "MARKET (fills now)" if market else f"@ {price} limit"
    print(f"\n⚠️  ABOUT TO PLACE A {'TESTNET (fake money)' if testnet else 'LIVE — REAL MONEY'} ORDER:")
    print(f"   SHORT {qty} {coin} {kind} | SL {sl} | TP {tp}")
    if input('   Type "CONFIRM" to place, anything else to abort: ').strip() != "CONFIRM":
        print("   Aborted — no order placed."); return

    try:
        ex.set_leverage(o["leverage"], sym)
    except Exception as e:
        print(f"   (leverage set skipped: {str(e)[:70]})")
    try:
        if market:
            order = ex.create_order(sym, "market", "sell", float(qty), None,
                                    params={"stopLoss": sl, "takeProfit": tp, "positionIdx": 0})
        else:
            order = ex.create_order(sym, "limit", "sell", float(qty), float(price),
                                    params={"stopLoss": sl, "takeProfit": tp, "positionIdx": 0})
        print(f"\n✅ Order placed: id {order.get('id')}  status {order.get('status')}")
        print(f"   {'Filled at market — check your position + SL/TP on Bybit.' if market else 'Resting limit — check on Bybit.'}")
    except Exception as e:
        print(f"\n✗ Order rejected: {e}")

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
    ap.add_argument("--market", action="store_true", help="fill NOW at market (no waiting for a limit to fill)")
    a = ap.parse_args()
    coin = a.coin.upper().replace("USDT", "")

    cur = bybit_price(coin, a.testnet)
    # --market always fills at current price, so size off current (ignore --entry)
    entry = cur if a.market else (a.entry if a.entry else cur)
    tp = a.tp if a.tp else pre_pump_base(coin, a.testnet)
    prem = premium_snapshot(coin) if not a.testnet else []

    tag = "MARKET — fills now" if a.market else (f"entry {entry:.6g}" if a.entry else "entry = current")
    print(f"\n{coin}: live {cur:.6g}   ({tag})")
    if not tp:
        print("✗ couldn't compute TP — pass --tp"); return
    o = build(coin, entry, a.sl_pct, tp, a.risk, a.leverage)
    ticket(coin, o, prem)

    if a.execute:
        execute(coin, o, a.testnet, a.market)
    else:
        flags = f"--entry {entry:.6g} --tp {tp:.6g}"
        print(f"\n[DRY RUN] to place:  python prepare_trade.py {coin} {flags} --execute")
        print(f"          test first:  ... --execute --testnet\n")

if __name__ == "__main__":
    main()
