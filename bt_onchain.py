#!/usr/bin/env python3
"""
Enrich backtest events (bt_events.json) with the Nansen on-chain manipulation read
AT the historical entry timestamp — the same signal the live bot annotates, but
queried for the 24h window ending at each past pump so we can test it as an ENTRY factor.

Reuses auto_trader._resolve_contract (Bybit coin-info -> CMC -> CoinGecko) so the
backtest uses the identical ticker->contract resolution as production.

  net<0  -> DIST  (top-100 holders distributing into the pump = manipulation confirmed)
  net>=0 -> ACCUM
  unresolved / untracked -> no-data

Idempotent: skips events already enriched; caches resolution per coin; saves after each.
"""
import os, json, time, requests
from datetime import datetime, timezone, timedelta
import auto_trader as A                              # reuse the live resolver + chain maps

HERE = os.path.dirname(os.path.abspath(__file__))
EVF = os.path.join(HERE, "bt_events.json")
KEY = os.environ.get("NANSEN_API_KEY")

class Retry(Exception): pass

def nansen_net(chain, addr, ts_ms):
    """Returns net flow (int/float) or None for empty/untracked. Raises Retry on network failure."""
    now = datetime.fromtimestamp(ts_ms / 1000, timezone.utc)
    frm = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"); to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    for attempt in range(4):
        try:
            r = requests.post("https://api.nansen.ai/api/v1/tgm/flows",
                headers={"apiKey": KEY, "Content-Type": "application/json"},
                json={"chain": chain, "token_address": addr, "date": {"from": frm, "to": to},
                      "label": "top_100_holders"}, timeout=40)
        except Exception:
            time.sleep(3 * (attempt + 1)); continue           # backoff then retry
        if r.status_code == 429:
            time.sleep(5 * (attempt + 1)); continue           # rate limited
        if r.status_code != 200: return None
        d = r.json().get("data", [])
        if not d: return None
        return sum((x.get("total_inflows_count") or 0) + (x.get("total_outflows_count") or 0) for x in d)
    raise Retry("nansen unreachable after retries")

def main():
    evs = json.load(open(EVF))
    resolved = {}                                    # coin -> (chain, addr) cache
    done = sum(1 for e in evs if "oc" in e)
    print(f"{len(evs)} events, {done} already enriched", flush=True)
    for k, e in enumerate(evs):
        if "oc" in e: continue
        coin = e["coin"]
        if coin not in resolved:
            resolved[coin] = A._resolve_contract(coin)
        chain, addr = resolved[coin]
        if not addr:
            e.update({"oc": "no-data", "net": None})
        else:
            try:
                net = nansen_net(chain, addr, e["ts"])
            except Retry:
                print(f"  [{k+1}/{len(evs)}] {coin:10} network fail — left pending, re-run to finish", flush=True)
                continue                             # leave unenriched so a re-run retries it
            if net is None:
                e.update({"oc": "no-data", "net": None})
            else:
                e.update({"oc": "DIST" if net < 0 else "ACCUM", "net": int(net)})
            time.sleep(1.1)                          # Nansen rate limit
        json.dump(evs, open(EVF, "w"))               # incremental save
        print(f"  [{k+1}/{len(evs)}] {coin:10} {datetime.fromtimestamp(e['ts']/1000,timezone.utc):%Y-%m-%d} -> {e['oc']}"
              + (f" (net{e['net']:+d})" if e.get('net') is not None else ""), flush=True)
    res = {}
    for e in evs: res[e["oc"]] = res.get(e["oc"], 0) + 1
    print("done. coverage:", res)

if __name__ == "__main__":
    if not KEY: print("Set NANSEN_API_KEY"); raise SystemExit(1)
    main()
