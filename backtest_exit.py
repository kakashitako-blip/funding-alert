#!/usr/bin/env python3
"""
Rigorous exit-rule backtest. Entry is fixed; we test which EXIT wins.

Entry (mechanical): premium crosses down through -1.5% while price is within
15% of its 3-day high (a pump-fade, not a downtrend). Stop = 3d-high x1.02.
Premium reconstructed from Bybit mark vs index 15m klines: (mark-index)/index.

Exit rules tested head-to-head from the same entry/stop:
  hold       hold to pre-pump base TP (the greedy baseline)
  flip       cover when premium recovers to >= -0.3%
  flip_early cover when premium recovers to >= -0.8% (half the dip)
  pt8        take profit at +8% price move (+32% ROI @4x)
  pt12       take profit at +12% price (+48% ROI)
  trail4     once >3% in profit, exit if price rebounds 4% off the best low
  cascade6   quick scalp: take +6% price (+24% ROI)
All rules share the same hard stop (squeeze protection).
"""
import requests, statistics, time, sys

BYBIT = "https://api.bybit.com"
LEV = 4
INT = "15"                 # 15-minute bars
LOOK = 288                 # ~3 days of 15m bars
FWD = 288                  # simulate up to ~3 days forward
DAYS = 30
RULES = ["hold", "flip", "flip_early", "pt8", "pt12", "trail4", "cascade6"]
COINS = ["POWR","ACT","AGLD","ARK","ESPORTS","KNC","ORDER","TOSHI","MMT","AMAT","NBIS",
         "DFIN","GOAT","PNUT","MOODENG","AI16Z","GRIFFAIN","ZEREBRO","ARC","SWARMS",
         "FARTCOIN","BAN","CHILLGUY","HIPPO","NEIRO","POPCAT","MEW","TURBO","ZEC","XPL"]

def kl_paged(sym, kind, days=DAYS):
    ep = {"price":"/v5/market/kline","mark":"/v5/market/mark-price-kline",
          "index":"/v5/market/index-price-kline"}[kind]
    need = (days*24*60)//int(INT)
    out, end = {}, None
    for _ in range((need//1000)+2):
        p = {"category":"linear","symbol":sym,"interval":INT,"limit":1000}
        if end: p["end"] = end
        try:
            rows = requests.get(BYBIT+ep, params=p, timeout=15).json().get("result",{}).get("list",[])
        except Exception:
            break
        if not rows: break
        for r in rows: out[int(r[0])] = r
        end = int(rows[-1][0]) - 1
        time.sleep(0.07)
        if len(out) >= need: break
    return out

def series(coin):
    sym = coin+"USDT"
    P, M, I = kl_paged(sym,"price"), kl_paged(sym,"mark"), kl_paged(sym,"index")
    ts = sorted(set(P) & set(M) & set(I))
    bars = []
    for t in ts:
        ix = float(I[t][4])
        prem = ((float(M[t][4]) - ix) / ix * 100) if ix > 0 else None
        bars.append({"h":float(P[t][2]),"l":float(P[t][3]),"c":float(P[t][4]),"prem":prem})
    return bars

def simulate(bars, i, entry, stop, tp):
    exits, best_low = {}, entry
    end = min(i+FWD, len(bars))
    for j in range(i+1, end):
        bj = bars[j]; h,l,c,pr = bj["h"],bj["l"],bj["c"],bj["prem"]
        best_low = min(best_low, l)
        stopped = h >= stop
        for r in RULES:                       # stop pre-empts everything (conservative)
            if r not in exits and stopped: exits[r] = ("STOP", stop)
        if not stopped:
            if "hold" not in exits and l <= tp: exits["hold"] = ("TP", tp)
            if "flip" not in exits and pr is not None and pr >= -0.3: exits["flip"] = ("FLIP", c)
            if "flip_early" not in exits and pr is not None and pr >= -0.8: exits["flip_early"] = ("FLIP", c)
            if "pt8" not in exits and l <= entry*0.92: exits["pt8"] = ("PT", entry*0.92)
            if "pt12" not in exits and l <= entry*0.88: exits["pt12"] = ("PT", entry*0.88)
            if "cascade6" not in exits and l <= entry*0.94: exits["cascade6"] = ("PT", entry*0.94)
            if "trail4" not in exits and best_low < entry*0.97 and h >= best_low*1.04:
                exits["trail4"] = ("TRAIL", best_low*1.04)
        if len(exits) == len(RULES): break
    last = bars[end-1]["c"]
    for r in RULES:
        exits.setdefault(r, ("TIME", last))
    mae = (max(x["h"] for x in bars[i+1:end]) - entry) / entry * 100
    return {"entry":entry, "stop":stop, "exits":exits, "mae":mae}

def backtest(coin):
    bars = series(coin)
    cov = sum(1 for b in bars if b["prem"] is not None)
    if len(bars) < LOOK+50:
        return [], f"{len(bars)} bars — too few"
    ev, i = [], LOOK
    while i < len(bars)-5:
        b, prev = bars[i], bars[i-1]
        if b["prem"] is None or prev["prem"] is None: i += 1; continue
        rh = max(x["h"] for x in bars[i-LOOK:i+1])
        rl = min(x["l"] for x in bars[i-LOOK:i+1])
        pumped = rl > 0 and (rh / rl) >= 1.20          # REQUIRED: 20%+ pump in window
        # entry: 20%+ pump + negative funding (premium <=-1%) + price still near the high (fade)
        if pumped and b["prem"] <= -1.0 and prev["prem"] > -1.0 and b["c"] >= rh*0.85:
            entry, stop = b["c"], rh*1.02
            lows = sorted(x["l"] for x in bars[i-LOOK:i])
            tp = lows[int(len(lows)*0.15)]
            if tp < entry:
                e = simulate(bars, i, entry, stop, tp)
                e["coin"] = coin; e["pump"] = (rh/rl - 1)*100; e["eprem"] = b["prem"]
                ev.append(e)
                i += 96; continue
        i += 1
    return ev, f"{len(bars)} bars, {100*cov//max(1,len(bars))}% prem, {len(ev)} events"

def short_ret(entry, px): return (entry - px) / entry * 100

def build_universe(mc_lo=10e6, mc_hi=150e6, cap=70):
    """Low/mid-cap manipulation universe: Bybit perps ∩ CoinGecko $10-150M cap.
    Excludes large caps (ZEC etc.) and tokenized stocks (not in CG crypto list)."""
    perps, cursor = set(), None
    for _ in range(8):
        p = {"category": "linear", "limit": 1000}
        if cursor: p["cursor"] = cursor
        try:
            res = requests.get(BYBIT+"/v5/market/instruments-info", params=p, timeout=15).json().get("result", {})
        except Exception:
            break
        for it in res.get("list", []):
            s = it.get("symbol", "")
            if s.endswith("USDT") and it.get("quoteCoin") == "USDT":
                perps.add(s[:-4])
        cursor = res.get("nextPageCursor")
        if not cursor: break
    caps = {}
    for page in range(1, 6):
        try:
            arr = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page},
                timeout=25).json()
        except Exception:
            break
        for c in arr:
            sym = (c.get("symbol") or "").upper(); mc = c.get("market_cap") or 0
            if sym and mc: caps.setdefault(sym, mc)
        time.sleep(2)
    uni = sorted([(c, caps[c]) for c in perps if c in caps and mc_lo <= caps[c] <= mc_hi], key=lambda x: x[1])
    print(f"universe: {len(uni)} low/mid-cap perps in ${mc_lo/1e6:.0f}-{mc_hi/1e6:.0f}M (smallest first)")
    for c, mc in uni[:cap]:
        print(f"   {c:10} ${mc/1e6:5.1f}M")
    return [c for c, _ in uni[:cap]]

if __name__ == "__main__":
    if "--auto" in sys.argv:
        coins = build_universe()
    else:
        coins = [a for a in sys.argv[1:] if not a.startswith("-")] or COINS
    allev = []
    for c in coins:
        ev, note = backtest(c)
        print(f"{c:9} {note}")
        allev.extend(ev)
    print(f"\n{'='*64}\n{len(allev)} pump-fade events (20%+ pump + neg funding) across {len(coins)} coins\n{'='*64}")
    if not allev: sys.exit()
    print("events tested (coin | pump size | entry premium):")
    for e in sorted(allev, key=lambda x: -x.get("pump", 0)):
        print(f"   {e['coin']:9} +{e.get('pump',0):4.0f}% pump | prem {e.get('eprem',0):+.2f}%")
    print()
    print(f"{'rule':11} {'n':>3} {'win%':>5} {'avgROI':>8} {'medROI':>8} {'stop':>5} {'totalROI':>9}")
    scored = []
    for r in RULES:
        rs = [short_ret(e["entry"], e["exits"][r][1]) * LEV for e in allev]
        wins = sum(1 for x in rs if x > 0)
        stops = sum(1 for e in allev if e["exits"][r][0] == "STOP")
        avg, med, tot = statistics.mean(rs), statistics.median(rs), sum(rs)
        scored.append((avg, r))
        print(f"{r:11} {len(rs):>3} {100*wins//len(rs):>4}% {avg:>+7.1f}% {med:>+7.1f}% {stops:>5} {tot:>+8.1f}%")
    mae = statistics.mean(e["mae"] for e in allev)
    best = max(scored)
    print(f"\navg max-adverse-excursion: +{mae:.1f}% (typical squeeze against the short)")
    print(f"best exit rule by avg ROI: {best[1]} ({best[0]:+.1f}% per trade)")
