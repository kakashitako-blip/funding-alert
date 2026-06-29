#!/usr/bin/env python3
"""
Autonomous micro-trader for the manipulation-short strategy. TINY size, hard rails.

Entry (backtested): 20%+ PRIMARY pump (3d-high = 7d-high, no 2nd bounces) +
funding <= -1% + price near the peak (within 7%) + a rejection bar.
Exit: stop +20% / take-profit -12% (pt12), both ATTACHED to the order so Bybit
manages them — no babysitting.

Candidates come from Coinglass "Lowest Funding Rate" (Firecrawl), so each scan
only deep-checks the handful of coins already at deep negative funding.

Runs on the Mac (Bybit reachable). Safety:
  - $5 risk/trade (~$25 notional)        - max 2 concurrent positions
  - max 4 NEW trades per day             - kill switch: create file STOP_AUTO
  - pings every action to Telegram       - pure mechanical (no on-chain/news gate yet)

  python3 auto_trader.py --once     # ONE dry scan: show candidates + what it WOULD do, NO orders
  python3 auto_trader.py            # live loop (scans every 15 min)
"""
import os, time, json, sys
from datetime import datetime, timezone
import requests, ccxt

KEY = os.environ.get("BYBIT_API_KEY"); SEC = os.environ.get("BYBIT_API_SECRET")
TOKEN = os.environ.get("BOT_TOKEN"); CHAT = str(os.environ.get("CHAT_ID", ""))
FCKEY = os.environ.get("FIRECRAWL_API_KEY")
HERE = os.path.dirname(os.path.abspath(__file__))
POS = os.path.join(HERE, "positions.json"); STATE = os.path.join(HERE, "auto_state.json")
KILL = os.path.join(HERE, "STOP_AUTO")

RISK, STOP_PCT, TP_PCT, LEV = 5.0, 20.0, 12.0, 4
MAX_CONCURRENT, MAX_NEW_PER_DAY, SCAN_EVERY = 2, 4, 900
LOOK, LONG = 288, 672          # 3-day and 7-day windows (15m bars)

ex = ccxt.bybit({"apiKey": KEY, "secret": SEC, "options": {"defaultType": "linear"}})

def tg(msg):
    if not TOKEN: return
    try: requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                       json={"chat_id": CHAT, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except Exception: pass

def log(m): print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {m}", flush=True)

def klines(sym, kind, limit=700):
    ep = {"price": "/v5/market/kline", "mark": "/v5/market/mark-price-kline",
          "index": "/v5/market/index-price-kline"}[kind]
    try:
        r = requests.get("https://api.bybit.com" + ep, params={"category": "linear",
            "symbol": sym, "interval": "15", "limit": limit}, timeout=15).json()
        return list(reversed(r.get("result", {}).get("list", [])))
    except Exception:
        return []

def get_bars(coin):
    sym = coin + "USDT"
    P = {r[0]: r for r in klines(sym, "price")}
    M = {r[0]: float(r[4]) for r in klines(sym, "mark")}
    I = {r[0]: float(r[4]) for r in klines(sym, "index")}
    bars = []
    for t in sorted(P):
        if t in M and t in I and I[t] > 0:
            r = P[t]
            bars.append({"h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                         "prem": (M[t] - I[t]) / I[t] * 100})
    return bars

def entry_signal(bars):
    """Latest-bar check for the backtested primary-pump rejection entry."""
    if len(bars) < LONG + 5:
        return None
    i = len(bars) - 1
    b, prev = bars[i], bars[i - 1]
    rh = max(x["h"] for x in bars[i - LOOK:i + 1])
    rl = min(x["l"] for x in bars[i - LOOK:i + 1])
    long_high = max(x["h"] for x in bars[i - LONG:i + 1])
    if not (rl > 0 and rh / rl >= 1.20): return None          # 20%+ pump
    if rh < long_high * 0.97: return None                      # primary pump (not 2nd bounce)
    if b["c"] < rh * 0.93: return None                         # near the peak
    if b["c"] >= prev["c"]: return None                        # rejecting (turning down)
    if b["prem"] > -1.0: return None                           # negative funding
    return {"entry": b["c"], "pump": (rh / rl - 1) * 100, "prem": b["prem"]}

def candidates():
    """Coins currently at deep negative funding (Coinglass Lowest Funding box via Firecrawl)."""
    if not FCKEY: return []
    try:
        import re
        md = (requests.post("https://api.firecrawl.dev/v2/scrape",
              headers={"Authorization": f"Bearer {FCKEY}"},
              json={"url": "https://www.coinglass.com/FundingRate", "formats": ["markdown"],
                    "onlyMainContent": True, "waitFor": 8000, "proxy": "auto"}, timeout=60)
              .json().get("data", {}) or {}).get("markdown", "") or ""
    except Exception as e:
        log(f"candidates fetch failed: {str(e)[:50]}"); return []
    if "Lowest Funding Rate" not in md: return []
    sec = md[md.find("Lowest Funding Rate"):]
    end = sec.find("USDT or USD");  sec = sec[:end] if end > 0 else sec
    seen, out = set(), []
    for m in re.finditer(r"\[(\w+)\s+([A-Za-z0-9]+)/USDT[\\\s]*?(-?\d+\.\d+)%\]", sec):
        coin, rate = m.group(2), float(m.group(3))
        if rate <= -1.0 and coin not in seen:
            seen.add(coin); out.append(coin)
    return out

def held_coins():
    try:
        return {p["symbol"].split("/")[0] for p in ex.fetch_positions(params={"settleCoin": "USDT"}) if p.get("contracts")}
    except Exception:
        return set()

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

def save_state(s): json.dump(s, open(STATE, "w"), indent=2)

def git_sync():
    import subprocess
    for c in (["add", "positions.json"], ["commit", "-m", "auto_trader: position"], ["push"]):
        try: subprocess.run(["git", "-C", HERE] + c, capture_output=True, timeout=25)
        except Exception: pass

def place(coin, sig, dry):
    sym = f"{coin}/USDT:USDT"
    ex.load_markets()
    px = float(ex.fetch_ticker(sym)["last"])           # fresh price for SL/TP
    stop = px * (1 + STOP_PCT / 100); tp = px * (1 - TP_PCT / 100)
    notional = RISK / (STOP_PCT / 100)                 # $5 / 0.20 = $25
    qty_raw = notional / px
    mn = ex.markets[sym].get("limits", {}).get("amount", {}).get("min") or 0
    if qty_raw < mn:
        return f"skip {coin}: qty {qty_raw:.4g} < min {mn} (raise risk for this coin)"
    qty = ex.amount_to_precision(sym, qty_raw)
    line = (f"SHORT {coin} {qty} @~{px:.6g} | SL {stop:.6g} (+{STOP_PCT:.0f}%) | "
            f"TP {tp:.6g} (-{TP_PCT:.0f}%) | risk ${RISK:.0f} | pump +{sig['pump']:.0f}% prem {sig['prem']:.2f}%")
    if dry:
        return "WOULD " + line
    ex.set_leverage(LEV, sym)
    ex.create_order(sym, "market", "sell", float(qty), None,
                    params={"stopLoss": ex.price_to_precision(sym, stop),
                            "takeProfit": ex.price_to_precision(sym, tp), "positionIdx": 0})
    try:
        pos = [p for p in json.load(open(POS)) if p["coin"] != coin]
    except Exception:
        pos = []
    pos.append({"coin": coin, "side": "short", "entry": px, "sl": stop, "tp": tp, "auto": True})
    json.dump(pos, open(POS, "w"), indent=2); git_sync()
    return "✅ " + line

def scan(dry=False):
    if os.path.exists(KILL):
        log("KILL switch present — halted"); tg("🛑 <b>auto-trader halted</b> (STOP_AUTO file present)"); return
    st = load_state()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if st.get("day") != today: st = {"day": today, "new": 0}
    if st["new"] >= MAX_NEW_PER_DAY:
        log("daily new-trade cap reached"); return
    held = held_coins()
    if len(held) >= MAX_CONCURRENT:
        log(f"at max concurrent ({len(held)})"); return
    cands = candidates()
    log(f"candidates (funding<=-1%): {cands or 'none'}")
    fired = []
    for coin in cands:
        if coin in held: continue
        sig = entry_signal(get_bars(coin))
        if not sig:
            log(f"  {coin}: no entry signal"); continue
        msg = place(coin, sig, dry)
        log(f"  {coin}: {msg}")
        fired.append(msg)
        if not dry and msg.startswith("✅"):
            st["new"] += 1; tg("🤖 " + msg)
            if st["new"] >= MAX_NEW_PER_DAY or len(held) + st["new"] >= MAX_CONCURRENT: break
    save_state(st)
    return fired

if __name__ == "__main__":
    if not all([KEY, SEC]):
        print("Set BYBIT_API_KEY / BYBIT_API_SECRET"); sys.exit(1)
    if "--once" in sys.argv:
        log("DRY scan (no orders):")
        res = scan(dry=True)
        print("\nResult:", res or "nothing would fire this scan")
    else:
        tg(f"🤖 <b>Auto-trader LIVE</b> — ${RISK:.0f} risk, max {MAX_CONCURRENT} open, "
           f"{MAX_NEW_PER_DAY}/day. Stop +{STOP_PCT:.0f}% TP -{TP_PCT:.0f}%. Halt: /stopauto in Telegram.")
        halted = False
        while True:
            if os.path.exists(KILL):                       # paused (not killed) — resumes when file removed
                if not halted: tg("🛑 <b>auto-trader paused</b> (/startauto to resume)"); halted = True
                time.sleep(60); continue
            if halted: tg("▶️ <b>auto-trader resumed</b>"); halted = False
            try: scan(dry=False)
            except Exception as e: log(f"scan error: {str(e)[:80]}")
            time.sleep(SCAN_EVERY)
