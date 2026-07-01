#!/usr/bin/env python3
"""
Report the VOLUME-SURGE strategy: baseline vs. entering only when 24h volume-surge
exceeds a fixed multiple of the coin's baseline. Fixed thresholds (not tertiles) so it's
a real rule, not curve-fit. Scores pt12 (live exit rule) at 4x.

  python3 bt_volstrat.py
"""
import json, os, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
evs = json.load(open(os.path.join(HERE, "bt_events.json")))
LEV, RULE = 4, "pt12"

def sret(e):
    return (e["entry"] - e["exits"][RULE][1]) / e["entry"] * 100 * LEV

def line(name, group):
    if not group: return f"{name:26} {'0':>4}   —"
    rs = [sret(e) for e in group]
    wins = sum(1 for x in rs if x > 0)
    stops = sum(1 for e in group if e["exits"][RULE][0] == "STOP")
    coins = len(set(e["coin"] for e in group))
    return (f"{name:26} {len(group):>4}  {coins:>4}  {100*wins/len(group):>4.0f}%  "
            f"{statistics.mean(rs):>+7.1f}%  {100*stops/len(group):>4.0f}%  {sum(rs):>+9.0f}%")

hdr = f"{'strategy':26} {'evts':>4}  {'coin':>4}  {'win':>5}  {'avgROI':>7}  {'stop':>5}  {'total':>9}"
print(f"=== volume-surge strategy · pt12 · {len(evs)} events / {len(set(e['coin'] for e in evs))} coins ===\n")
print(hdr); print("-" * len(hdr))
print(line("all (no filter)", evs))
print()
for thr in [5, 8, 10, 12, 15, 18, 20, 25]:
    print(line(f"vsurge > {thr}x", [e for e in evs if e.get("vsurge", 0) > thr]))
print("\nevts = trades taken, coin = distinct coins. Higher threshold = fewer but cleaner trades.")
