#!/usr/bin/env python3
"""
Funding Rate Alert Bot
Scans funding rates across major CEXs every 15 minutes.

Two alert modes:
1. SCAN: fires when any coin's funding enters the -1.5% to -2.5% zone
2. WATCHLIST: fires on ANY funding change for coins you're trading/considering

Runs as a long-lived process (for cloud deploy) or one-shot via --once.
"""

import requests
import json
import os
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.txt")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8928254387:AAGNfwUxrbmCQgV-Y4dxvwrPnvlCZI3hZeo")
CHAT_ID = os.environ.get("CHAT_ID", "1323857029")

SCAN_LOW = -0.025   # -2.5%
SCAN_HIGH = -0.015  # -1.5%
WATCH_DELTA = 0.002 # alert on 0.2% change for watchlist coins
INTERVAL = 900      # 15 minutes


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_telegram(message):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return r.json().get("ok", False)
    except Exception as e:
        log(f"Telegram error: {e}")
        return False


def load_watchlist():
    if not os.path.exists(WATCHLIST_FILE):
        return set()
    with open(WATCHLIST_FILE) as f:
        return {line.strip().upper() for line in f if line.strip() and not line.startswith("#")}


# ── Exchange fetchers ────────────────────────────────────────────

def fetch_binance():
    try:
        data = requests.get(
            "https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10
        ).json()
        out = []
        for item in data:
            sym = item["symbol"]
            if not sym.endswith("USDT"):
                continue
            out.append({
                "coin": sym[:-4],
                "exchange": "Binance",
                "rate": float(item.get("lastFundingRate", 0)),
                "interval": "8h",
            })
        return out
    except Exception as e:
        log(f"Binance fetch error: {e}")
        return []


def fetch_bybit():
    try:
        data = requests.get(
            "https://api.bybit.com/v5/market/tickers?category=linear", timeout=10
        ).json()["result"]["list"]
        out = []
        for item in data:
            sym = item["symbol"]
            if not sym.endswith("USDT"):
                continue
            out.append({
                "coin": sym[:-4],
                "exchange": "Bybit",
                "rate": float(item.get("fundingRate", 0)),
                "interval": "8h",
            })
        return out
    except Exception as e:
        log(f"Bybit fetch error: {e}")
        return []


def fetch_mexc():
    try:
        data = requests.get(
            "https://contract.mexc.com/api/v1/contract/ticker", timeout=10
        ).json().get("data", [])
        out = []
        for item in data:
            sym = item.get("symbol", "")
            if not sym.endswith("_USDT"):
                continue
            out.append({
                "coin": sym[:-5],
                "exchange": "MEXC",
                "rate": float(item.get("fundingRate", 0)),
                "interval": "8h",
            })
        return out
    except Exception as e:
        log(f"MEXC fetch error: {e}")
        return []


def fetch_kucoin():
    try:
        data = requests.get(
            "https://api-futures.kucoin.com/api/v1/contracts/active", timeout=10
        ).json().get("data", [])
        out = []
        for item in data:
            sym = item.get("symbol", "")
            if not sym.endswith("USDTM"):
                continue
            gran_ms = int(item.get("fundingRateGranularity", 28800000))
            out.append({
                "coin": sym[:-5],
                "exchange": "KuCoin",
                "rate": float(item.get("fundingFeeRate", 0)),
                "interval": f"{gran_ms // 3600000}h",
            })
        return out
    except Exception as e:
        log(f"KuCoin fetch error: {e}")
        return []


def fetch_okx_watchlist(watchlist):
    out = []
    for coin in watchlist:
        try:
            r = requests.get(
                f"https://www.okx.com/api/v5/public/funding-rate?instId={coin}-USDT-SWAP",
                timeout=10,
            ).json()
            if r.get("data"):
                item = r["data"][0]
                out.append({
                    "coin": coin,
                    "exchange": "OKX",
                    "rate": float(item.get("fundingRate", 0)),
                    "interval": "4h",
                })
        except Exception as e:
            log(f"OKX {coin} error: {e}")
    return out


# ── State management ─────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"scan": {}, "watch": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Scan logic ───────────────────────────────────────────────────

def scan():
    now = datetime.now(timezone.utc)
    log("Scan starting")

    watchlist = load_watchlist()
    log(f"Watchlist: {watchlist or 'empty'}")

    all_rates = []
    for fetcher in [fetch_binance, fetch_bybit, fetch_mexc, fetch_kucoin]:
        all_rates.extend(fetcher())

    if watchlist:
        all_rates.extend(fetch_okx_watchlist(watchlist))

    log(f"Fetched {len(all_rates)} pairs")

    state = load_state()
    prev_scan = state.get("scan", {})
    prev_watch = state.get("watch", {})
    messages = []

    # ── MODE 1: Scan for funding in the -1.5% to -2.5% zone ─────
    in_zone = sorted(
        [r for r in all_rates if SCAN_LOW <= r["rate"] <= SCAN_HIGH],
        key=lambda x: x["rate"],
    )

    scan_new = []
    scan_gone = []
    current_scan_keys = {f"{r['exchange']}:{r['coin']}" for r in in_zone}

    for item in in_zone:
        key = f"{item['exchange']}:{item['coin']}"
        if key not in prev_scan:
            scan_new.append(item)

    for key in prev_scan:
        if key not in current_scan_keys:
            exchange, coin = key.split(":", 1)
            match = next(
                (r for r in all_rates if r["exchange"] == exchange and r["coin"] == coin),
                None,
            )
            rate_now = match["rate"] if match else 0
            direction = "deeper" if rate_now < SCAN_LOW else "recovered"
            scan_gone.append({"coin": coin, "exchange": exchange, "rate": rate_now, "direction": direction})

    if scan_new or scan_gone:
        parts = []
        if scan_new:
            parts.append("\U0001f534 <b>Entered -1.5% to -2.5% zone</b>")
            for a in scan_new:
                parts.append(
                    f"  <b>{a['coin']}</b> {a['exchange']}  "
                    f"<b>{a['rate']*100:.2f}%</b> / {a['interval']}"
                )
        if scan_gone:
            for g in scan_gone:
                emoji = "\U0001f53b" if g["direction"] == "deeper" else "✅"
                parts.append(
                    f"{emoji} <b>{g['coin']}</b> {g['exchange']} left zone "
                    f"({g['direction']}, now {g['rate']*100:.2f}%)"
                )
        parts.append(f"\n\U0001f4ca {len(in_zone)} pairs in zone  |  {now.strftime('%H:%M UTC')}")
        messages.append("\n".join(parts))

    # ── MODE 2: Watchlist — alert on any funding change ──────────
    if watchlist:
        watched = [r for r in all_rates if r["coin"] in watchlist]
        watch_alerts = []

        for item in watched:
            key = f"{item['exchange']}:{item['coin']}"
            prev_rate = prev_watch.get(key)
            if prev_rate is None:
                watch_alerts.append({**item, "change": "new"})
            elif abs(item["rate"] - prev_rate) >= WATCH_DELTA:
                watch_alerts.append({
                    **item,
                    "prev_rate": prev_rate,
                    "change": "moved",
                })

        if watch_alerts:
            parts = ["\U0001f50d <b>Watchlist Update</b>"]
            for a in sorted(watch_alerts, key=lambda x: x["rate"]):
                if a["change"] == "new":
                    parts.append(
                        f"  <b>{a['coin']}</b> {a['exchange']}  "
                        f"<b>{a['rate']*100:.2f}%</b> / {a['interval']}"
                    )
                else:
                    arrow = "⬇️" if a["rate"] < a["prev_rate"] else "⬆️"
                    parts.append(
                        f"  {arrow} <b>{a['coin']}</b> {a['exchange']}  "
                        f"{a['prev_rate']*100:.2f}% → <b>{a['rate']*100:.2f}%</b> / {a['interval']}"
                    )
            parts.append(f"\n⏰ {now.strftime('%H:%M UTC')}")
            messages.append("\n".join(parts))

        state["watch"] = {f"{r['exchange']}:{r['coin']}": r["rate"] for r in watched}

    # ── Send ─────────────────────────────────────────────────────
    for msg in messages:
        if len(msg) > 4000:
            msg = msg[:3950] + "\n... (truncated)"
        if send_telegram(msg):
            log(f"Alert sent ({len(msg)} chars)")
        else:
            log("Failed to send alert")

    if not messages:
        log(f"No changes. {len(in_zone)} in zone, {len(watchlist)} watched.")

    state["scan"] = {f"{r['exchange']}:{r['coin']}": r["rate"] for r in in_zone}
    state["last_run"] = now.isoformat()
    save_state(state)

    log("Scan complete")


# ── Entry point ──────────────────────────────────────────────────

if __name__ == "__main__":
    if "--once" in sys.argv:
        scan()
    else:
        log("Funding Alert Bot started (15min loop)")
        send_telegram("✅ <b>Funding Alert Bot started</b>\nScanning every 15min across Binance/Bybit/MEXC/KuCoin + OKX watchlist")
        while True:
            try:
                scan()
            except Exception as e:
                log(f"Scan crashed: {e}")
            time.sleep(INTERVAL)
