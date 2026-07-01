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
from datetime import datetime, timezone, timedelta
import requests, ccxt

KEY = os.environ.get("BYBIT_API_KEY"); SEC = os.environ.get("BYBIT_API_SECRET")
TOKEN = os.environ.get("BOT_TOKEN"); CHAT = str(os.environ.get("CHAT_ID", ""))
FCKEY = os.environ.get("FIRECRAWL_API_KEY")
HERE = os.path.dirname(os.path.abspath(__file__))
POS = os.path.join(HERE, "positions.json"); STATE = os.path.join(HERE, "auto_state.json")
KILL = os.path.join(HERE, "STOP_AUTO")

RISK, STOP_PCT, TP_PCT, LEV = 10.0, 20.0, 12.0, 4
MAX_CONCURRENT, MAX_NEW_PER_DAY, SCAN_EVERY = 2, 4, 300   # 5-min scan: catch fast rejections
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
    if b["prem"] > 0: return None                              # anti-squeeze: skip if perp at a PREMIUM (funding gate already passed via candidate)
    return {"entry": b["c"], "pump": (rh / rl - 1) * 100, "prem": b["prem"]}

def candidates():
    """Bybit perps at deep negative FUNDING (direct from Bybit tickers — free, fresh,
    Bybit-specific since we trade Bybit). Funding is the real signal, not premium."""
    try:
        rows = requests.get("https://api.bybit.com/v5/market/tickers",
                            params={"category": "linear"}, timeout=15).json().get("result", {}).get("list", [])
    except Exception as e:
        log(f"candidates fetch failed: {str(e)[:50]}"); return []
    out = []
    for t in rows:
        sym = t.get("symbol", ""); fr = t.get("fundingRate")
        if sym.endswith("USDT") and fr:
            try:
                if float(fr) <= -0.01:                 # funding <= -1%
                    out.append((sym[:-4], float(fr) * 100))
            except Exception:
                pass
    out.sort(key=lambda x: x[1])                       # most negative first
    return [c for c, _ in out]

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

NANSEN_CHAINS = {"Ethereum": "ethereum", "Solana": "solana", "BNB Smart Chain": "bnb",
                 "Base": "base", "Arbitrum One": "arbitrum", "Polygon PoS": "polygon",
                 "Polygon": "polygon", "Avalanche C-Chain": "avalanche", "Optimism": "optimism"}
CG_TO_NANSEN = {"ethereum": "ethereum", "solana": "solana", "base": "base", "arbitrum-one": "arbitrum",
                "binance-smart-chain": "bnb", "polygon-pos": "polygon", "avalanche": "avalanche"}
CHAIN_ORDER = ["ethereum", "solana", "bnb", "base", "arbitrum", "polygon", "avalanche", "optimism"]
CMC_TO_NANSEN = {"Ethereum": "ethereum", "BNB Smart Chain (BEP20)": "bnb", "Solana": "solana",
                 "Polygon": "polygon", "Base": "base", "Arbitrum": "arbitrum",
                 "Avalanche C-Chain": "avalanche", "Optimism": "optimism"}

def _resolve_contract(coin):
    """Resolve Bybit ticker -> (nansen_chain, contract). Bybit coin-info (free, exact ticker) ->
    CoinMarketCap (reliable symbol->contract, correct e.g. ID=SPACE ID, has ONG) -> CoinGecko (free fallback)."""
    try:
        rows = ex.private_get_v5_asset_coin_query_info({"coin": coin}).get("result", {}).get("rows", [])
        found = {}
        for c in (rows[0].get("chains", []) if rows else []):
            ns = NANSEN_CHAINS.get(c.get("chainType", "")); a = c.get("contractAddress", "")
            if ns and a: found[ns] = a
        for ns in CHAIN_ORDER:
            if found.get(ns): return ns, found[ns]
    except Exception:
        pass
    cmc = os.environ.get("CMC_API_KEY")                                  # 2. CoinMarketCap
    if cmc:
        try:
            coins = (requests.get("https://pro-api.coinmarketcap.com/v2/cryptocurrency/info",
                headers={"X-CMC_PRO_API_KEY": cmc}, params={"symbol": coin}, timeout=10)
                .json().get("data", {}).get(coin)) or []
            found = {}
            for x in ((coins[0].get("contract_address") if coins else []) or []):
                ns = CMC_TO_NANSEN.get((x.get("platform") or {}).get("name", "")); a = x.get("contract_address", "")
                if ns and a: found[ns] = a
            for ns in CHAIN_ORDER:
                if found.get(ns): return ns, found[ns]
        except Exception:
            pass
    try:
        cs = requests.get("https://api.coingecko.com/api/v3/search", params={"query": coin}, timeout=8).json().get("coins", [])
        m = next((c for c in cs if c.get("symbol", "").upper() == coin.upper()), None)   # exact only, never guess
        if m:
            d = requests.get(f"https://api.coingecko.com/api/v3/coins/{m['id']}",
                params={"localization": "false", "tickers": "false", "market_data": "false",
                        "community_data": "false", "developer_data": "false"}, timeout=8).json()
            plats = {k: v for k, v in (d.get("platforms") or {}).items() if v}
            for cg, ns in CG_TO_NANSEN.items():
                if plats.get(cg): return ns, plats[cg]
    except Exception:
        pass
    return None, None

def onchain_signal(coin):
    """SHADOW manipulation read via Nansen tgm/flows: 24h net flow of top-100 holders.
    Net OUTFLOW = insiders distributing into the pump = manipulation confirmed. Non-blocking."""
    key = os.environ.get("NANSEN_API_KEY")
    try:
        chain, addr = _resolve_contract(coin)
        if not addr: return "onchain=no-data(unresolved)"
        if not key: return "onchain=no-key"
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"); to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        r = requests.post("https://api.nansen.ai/api/v1/tgm/flows",
            headers={"apiKey": key, "Content-Type": "application/json"},
            json={"chain": chain, "token_address": addr, "date": {"from": frm, "to": to},
                  "label": "top_100_holders"}, timeout=15)
        if r.status_code != 200: return f"onchain=no-data(nansen{r.status_code})"
        data = r.json().get("data", [])
        if not data: return "onchain=no-data(untracked)"
        net = sum((x.get("total_inflows_count") or 0) + (x.get("total_outflows_count") or 0) for x in data)
        return f"onchain={'DISTRIBUTING' if net < 0 else 'accumulating'}(net{net:+.0f})"
    except Exception:
        return "onchain=no-data"

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
        return "WOULD " + line + " | " + onchain_signal(coin)
    ex.set_leverage(LEV, sym)
    ex.create_order(sym, "market", "sell", float(qty), None,
                    params={"stopLoss": ex.price_to_precision(sym, stop),
                            "takeProfit": ex.price_to_precision(sym, tp), "positionIdx": 0})
    try:
        pos = [p for p in json.load(open(POS)) if p["coin"] != coin]
    except Exception:
        pos = []
    pos.append({"coin": coin, "side": "short", "entry": px, "sl": stop, "tp": tp, "auto": True})
    oc = onchain_signal(coin)                          # SHADOW: annotate, does NOT block
    json.dump(pos, open(POS, "w"), indent=2); git_sync()
    return "✅ " + line + " | " + oc

def log_trade(p, exitpx, pnl):
    import csv
    entry = p.get("entry") or 0
    roi = ((entry - exitpx) / entry * 100 * LEV) if entry and exitpx else 0
    row = [datetime.now(timezone.utc).strftime("%Y-%m-%d"), p["coin"], "short", f"{entry:.6g}",
           f"{exitpx:.6g}", f"{LEV}x", f"{roi:+.2f}", f"{pnl:+.2f}",
           "WIN" if pnl >= 0 else "LOSS", "auto"]
    try:
        with open(os.path.join(HERE, "trades.csv"), "a", newline="") as f:
            csv.writer(f).writerow(row)
    except Exception: pass

def check_exits():
    """Detect auto-positions that closed (TP or SL), report P&L, log, and clean up."""
    try:
        open_now = {pp["symbol"].split("/")[0] for pp in ex.fetch_positions(params={"settleCoin": "USDT"}) if pp.get("contracts")}
    except Exception:
        return
    try: tracked = json.load(open(POS))
    except Exception: tracked = []
    still, changed = [], False
    for p in tracked:
        coin = p["coin"]
        if coin in open_now or not p.get("auto"):
            still.append(p); continue           # still open, or a manual position (leave it)
        changed = True
        pnl = exitpx = None
        try:
            lst = ex.private_get_v5_position_closed_pnl(
                {"category": "linear", "symbol": coin + "USDT", "limit": 1}).get("result", {}).get("list", [])
            if lst:
                pnl = float(lst[0].get("closedPnl") or 0); exitpx = float(lst[0].get("avgExitPrice") or 0)
        except Exception: pass
        if pnl is not None:
            entry = p.get("entry") or 0
            roi = ((entry - exitpx) / entry * 100 * LEV) if entry and exitpx else 0
            tag = "🎯 <b>TP HIT</b>" if pnl >= 0 else "🛑 <b>STOP HIT</b>"
            tg(f"{tag} — {coin} closed | <b>{pnl:+.2f} USDT</b> ({roi:+.0f}% ROI) | exit {exitpx:.6g}")
            log_trade(p, exitpx, pnl); log(f"{coin} closed {pnl:+.2f} USDT")
        else:
            tg(f"ℹ️ <b>{coin}</b> auto-position closed (P&L lookup failed — check /status)")
    if changed:
        json.dump(still, open(POS, "w"), indent=2); git_sync()

def scan(dry=False):
    if os.path.exists(KILL):
        log("KILL switch present — halted"); tg("🛑 <b>auto-trader halted</b> (STOP_AUTO file present)"); return
    if not dry: check_exits()                    # report TP/SL closes before scanning for new entries
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
