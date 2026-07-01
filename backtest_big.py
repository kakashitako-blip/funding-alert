#!/usr/bin/env python3
"""
Scaled 90-day backtest, CHUNKED so each run fits the time limit and accumulates.

Entry = bot's logic: 20%+ PRIMARY pump + funding<=-1% + near-peak + rejection.
Stop 20% (4x). Funding from Bybit funding-history (paginated to cover 90d).
Exits: hold / pt8 / pt12 / cascade6 / trail4 (bot uses pt12).

  python3 backtest_big.py --build              # build+cache the coin universe
  python3 backtest_big.py --slice 0 75         # process coins[0:75], append events
  python3 backtest_big.py --slice 75 150       # next chunk … etc
  python3 backtest_big.py --agg                # aggregate all accumulated events
"""
import requests, statistics, time, sys, bisect, json, os

BYBIT = "https://api.bybit.com"
LEV, INT, LOOK, LONG, FWD, DAYS = 4, "15", 288, 672, 288, 365
RULES = ["hold", "pt8", "pt12", "pt20", "cascade6", "trail4"]
HERE = os.path.dirname(os.path.abspath(__file__))
UNI = os.path.join(HERE, "bt_universe.json")
EVF = os.path.join(HERE, "bt_events.json")

def kl_paged(sym, days=DAYS):
    need = (days * 24 * 60) // int(INT); out = {}; end = None
    for _ in range((need // 1000) + 2):
        p = {"category": "linear", "symbol": sym, "interval": INT, "limit": 1000}
        if end: p["end"] = end
        try:
            rows = requests.get(BYBIT + "/v5/market/kline", params=p, timeout=15).json().get("result", {}).get("list", [])
        except Exception:
            break
        if not rows: break
        for r in rows: out[int(r[0])] = r
        end = int(rows[-1][0]) - 1; time.sleep(0.04)
        if len(out) >= need: break
    return out

def funding_hist(sym):
    out, end = [], None
    for _ in range(8):                       # paginate ~1600 records to cover 365d
        p = {"category": "linear", "symbol": sym, "limit": 200}
        if end: p["endTime"] = end
        try:
            rows = requests.get(BYBIT + "/v5/market/funding/history", params=p, timeout=15).json().get("result", {}).get("list", [])
        except Exception:
            break
        if not rows: break
        for x in rows: out.append((int(x["fundingRateTimestamp"]), float(x["fundingRate"]) * 100))
        end = int(rows[-1]["fundingRateTimestamp"]) - 1; time.sleep(0.04)
        if len(rows) < 200: break
    return sorted(out)

def series(coin):
    sym = coin + "USDT"; P = kl_paged(sym); fh = funding_hist(sym)
    if len(P) < LONG + 50 or not fh: return []
    fts = [t for t, _ in fh]; bars = []
    for t in sorted(P):
        r = P[t]; i = bisect.bisect_right(fts, int(t)) - 1
        bars.append({"t": int(t), "h": float(r[2]), "l": float(r[3]), "c": float(r[4]),
                     "v": float(r[5]), "fund": fh[i][1] if i >= 0 else None})
    return bars

def simulate(bars, i, entry, stop, tp):
    exits = {}; best_low = entry; end = min(i + FWD, len(bars))
    for j in range(i + 1, end):
        bj = bars[j]; h, l = bj["h"], bj["l"]; best_low = min(best_low, l); stopped = h >= stop
        for r in RULES:
            if r not in exits and stopped: exits[r] = ("STOP", stop)
        if not stopped:
            if "hold" not in exits and l <= tp: exits["hold"] = ("TP", tp)
            if "pt8" not in exits and l <= entry * 0.92: exits["pt8"] = ("PT", entry * 0.92)
            if "pt12" not in exits and l <= entry * 0.88: exits["pt12"] = ("PT", entry * 0.88)
            if "pt20" not in exits and l <= entry * 0.80: exits["pt20"] = ("PT", entry * 0.80)
            if "cascade6" not in exits and l <= entry * 0.94: exits["cascade6"] = ("PT", entry * 0.94)
            if "trail4" not in exits and best_low < entry * 0.97 and h >= best_low * 1.04:
                exits["trail4"] = ("TRAIL", best_low * 1.04)
        if len(exits) == len(RULES): break
    last = bars[end - 1]["c"]
    for r in RULES: exits.setdefault(r, ("TIME", last))
    return {"entry": entry, "exits": exits, "mae": (max(x["h"] for x in bars[i + 1:end]) - entry) / entry * 100}

def backtest(coin):
    bars = series(coin)
    if not bars: return []
    ev, i = [], LOOK
    while i < len(bars) - 5:
        b, prev = bars[i], bars[i - 1]
        if b["fund"] is None: i += 1; continue
        rh = max(x["h"] for x in bars[i - LOOK:i + 1]); rl = min(x["l"] for x in bars[i - LOOK:i + 1])
        long_high = max(x["h"] for x in bars[max(0, i - LONG):i + 1])
        if (rl > 0 and rh / rl >= 1.20 and rh >= long_high * 0.97 and b["c"] >= rh * 0.93
                and b["c"] < prev["c"] and b["fund"] <= -1.0):
            entry = b["c"]; stop = entry * 1.20
            lows = sorted(x["l"] for x in bars[i - LOOK:i]); tp = lows[int(len(lows) * 0.15)]
            if tp < entry:
                e = simulate(bars, i, entry, stop, tp)
                base = [x["v"] for x in bars[i - LOOK:i - 96]]; recent = [x["v"] for x in bars[i - 96:i]]
                bm = (sum(base) / len(base)) if base else 0
                vsurge = round(sum(recent) / len(recent) / bm, 2) if (bm > 0 and recent) else 0  # 24h vol vs prior baseline
                ev.append({"coin": coin, "ts": bars[i]["t"], "entry": entry, "exits": e["exits"],
                           "mae": e["mae"], "vsurge": vsurge}); i += 96; continue
        i += 1
    return ev

def build_universe(mc_lo=2e6, mc_hi=350e6, cap=700):
    perps, cursor = set(), None
    for _ in range(8):
        p = {"category": "linear", "limit": 1000}
        if cursor: p["cursor"] = cursor
        try: res = requests.get(BYBIT + "/v5/market/instruments-info", params=p, timeout=15).json().get("result", {})
        except Exception: break
        for it in res.get("list", []):
            s = it.get("symbol", "")
            if s.endswith("USDT") and it.get("quoteCoin") == "USDT": perps.add(s[:-4])
        cursor = res.get("nextPageCursor")
        if not cursor: break
    caps = {}
    for page in range(1, 7):
        arr = None
        for attempt in range(4):                     # retry on CoinGecko rate limits (429 -> non-list body)
            try: arr = requests.get("https://api.coingecko.com/api/v3/coins/markets",
                    params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page}, timeout=25).json()
            except Exception: arr = None
            if isinstance(arr, list): break
            time.sleep(6 * (attempt + 1))
        if not isinstance(arr, list): continue
        for c in arr:
            if not isinstance(c, dict): continue
            sym = (c.get("symbol") or "").upper(); mc = c.get("market_cap") or 0
            if sym and mc: caps.setdefault(sym, mc)
        time.sleep(2)
    uni = sorted([(c, caps[c]) for c in perps if c in caps and mc_lo <= caps[c] <= mc_hi], key=lambda x: x[1])
    return [c for c, _ in uni[:cap]]

def sret(entry, px): return (entry - px) / entry * 100

def agg(evs):
    print(f"=== {len(evs)} events (90d, 20%+ primary pump + funding<=-1%, 20% stop) ===")
    if not evs: return
    print(f"{'rule':9} {'n':>3} {'win%':>5} {'avgROI':>8} {'medROI':>8} {'stop':>5} {'total':>9}")
    for r in RULES:
        rs = [sret(e["entry"], e["exits"][r][1]) * LEV for e in evs]
        wins = sum(1 for x in rs if x > 0); stops = sum(1 for e in evs if e["exits"][r][0] == "STOP")
        print(f"{r:9} {len(rs):>3} {100*wins//len(rs):>4}% {statistics.mean(rs):>+7.1f}% {statistics.median(rs):>+7.1f}% {stops:>5} {sum(rs):>+8.1f}%")
    print(f"\navg MAE: +{statistics.mean(e['mae'] for e in evs):.1f}% | {len(set(e['coin'] for e in evs))} distinct coins")

if __name__ == "__main__":
    if "--build" in sys.argv:
        u = build_universe(); json.dump(u, open(UNI, "w"))
        if os.path.exists(EVF): os.remove(EVF)
        print(f"universe cached: {len(u)} coins; events reset")
    elif "--agg" in sys.argv:
        agg(json.load(open(EVF)) if os.path.exists(EVF) else [])
    elif "--slice" in sys.argv:
        k = sys.argv.index("--slice"); a, b = int(sys.argv[k + 1]), int(sys.argv[k + 2])
        coins = json.load(open(UNI))[a:b]
        evs = json.load(open(EVF)) if os.path.exists(EVF) else []
        print(f"slice {a}:{b} — {len(coins)} coins", flush=True)
        for c in coins:
            ev = backtest(c)
            if ev:
                evs.extend(ev); print(f"  {c}: {len(ev)}", flush=True)
                json.dump(evs, open(EVF, "w"))          # incremental save — timeout-safe
        json.dump(evs, open(EVF, "w"))
        print(f"done. total events now: {len(evs)}")
