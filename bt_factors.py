#!/usr/bin/env python3
"""
Evaluate on-chain (Nansen DIST/ACCUM) + volume-surge as ENTRY factors on top of the
mechanical backtest. Scores pt12 (the live exit rule) so results are comparable to the
existing backtest. For each factor group: n, win%, avg ROI, stop%, total ROI.

  python3 bt_factors.py
"""
import json, os, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
evs = json.load(open(os.path.join(HERE, "bt_events.json")))
LEV = 4
RULE = "pt12"

def sret(e):                                         # short ROI at 4x for the pt12 exit
    return (e["entry"] - e["exits"][RULE][1]) / e["entry"] * 100 * LEV

def row(name, group):
    if not group: return f"{name:22} {'0':>4}  —"
    rs = [sret(e) for e in group]
    wins = sum(1 for x in rs if x > 0)
    stops = sum(1 for e in group if e["exits"][RULE][0] == "STOP")
    return (f"{name:22} {len(group):>4}  {100*wins/len(group):>4.0f}%  {statistics.mean(rs):>+7.1f}%  "
            f"{100*stops/len(group):>4.0f}%  {sum(rs):>+8.0f}%")

hdr = f"{'group':22} {'n':>4}  {'win':>5}  {'avgROI':>7}  {'stop':>5}  {'total':>8}"
print(f"=== pt12 (SL+20% / TP-12%, 4x) by entry factor — {len(evs)} events ===\n")
print(hdr); print("-" * len(hdr))
print(row("ALL (baseline)", evs))
print()

# --- on-chain manipulation (Nansen) ---
for lab in ["DIST", "ACCUM", "no-data"]:
    print(row(f"onchain={lab}", [e for e in evs if e.get("oc") == lab]))
resolved = [e for e in evs if e.get("oc") in ("DIST", "ACCUM")]
print(row("  (resolved only)", resolved))
print()

# --- volume surge (24h vol vs prior baseline) ---
vs = sorted(e.get("vsurge", 0) for e in evs if e.get("vsurge"))
if vs:
    lo, hi = vs[len(vs)//3], vs[2*len(vs)//3]
    print(f"volume-surge tertiles: low<{lo:.1f}  mid  high>{hi:.1f}")
    print(row(f"vsurge low (<{lo:.1f}x)", [e for e in evs if 0 < e.get("vsurge", 0) <= lo]))
    print(row(f"vsurge mid", [e for e in evs if lo < e.get("vsurge", 0) <= hi]))
    print(row(f"vsurge high (>{hi:.1f}x)", [e for e in evs if e.get("vsurge", 0) > hi]))
    print()

    # --- combined: manipulation confirmed AND heavy volume ---
    print(row("DIST + vsurge>median", [e for e in evs if e.get("oc") == "DIST" and e.get("vsurge", 0) > vs[len(vs)//2]]))
    print(row("DIST + vsurge>high", [e for e in evs if e.get("oc") == "DIST" and e.get("vsurge", 0) > hi]))

print("\nRead: does gating on DIST (and/or high volume) lift win% & avgROI above the ALL baseline,"
      "\nand is the trade count still enough to matter? no-data = couldn't resolve the contract.")
