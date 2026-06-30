#!/usr/bin/env python3
"""
Funding Rate Alert Bot — powered by Coinglass scraping.
Scans every 15 minutes, alerts via Telegram.

Two modes:
1. SCAN: "Lowest Funding Rate" box → alert when any coin enters -1.5% to -2.5%
2. WATCHLIST: per-coin Coinglass page → alert on 0.2%+ change across all exchanges
"""

import requests
import json
import os
import sys
import re
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, "state.json")
WATCHLIST_FILE = os.path.join(SCRIPT_DIR, "watchlist.txt")
PRICE_ALERTS_FILE = os.path.join(SCRIPT_DIR, "price_alerts.json")
POSITIONS_FILE = os.path.join(SCRIPT_DIR, "positions.json")

# Credentials come from env (GitHub Secrets). No hardcoded fallback — repo is public.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

SCAN_LOW = -2.5
SCAN_HIGH = -1.5
WATCH_DELTA = 0.2
PREMIUM_ALERT = -2.0   # MEXC premium-index early-warning threshold (%) — leads funding


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_telegram(message, buttons=None):
    try:
        payload = {
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons:                      # inline keyboard: [[{text, callback_data}], ...]
            payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json=payload,
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


def load_price_alerts():
    """Manual price-level alerts for active trade setups.
    JSON list of {coin, level, direction(above|below), note}."""
    if not os.path.exists(PRICE_ALERTS_FILE):
        return []
    try:
        with open(PRICE_ALERTS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def load_positions():
    """Open positions to monitor for exit signals.
    JSON list of {coin, side, entry, sl, tp}."""
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def mexc_premium_price(coin):
    """(premium%, price) from MEXC ticker — works from GitHub Actions."""
    try:
        t = requests.get("https://contract.mexc.com/api/v1/contract/ticker",
                         params={"symbol": f"{coin}_USDT"}, timeout=8).json().get("data", {})
        price = float(t.get("lastPrice") or 0)
        fair = float(t.get("fairPrice") or 0)
        idx = float(t.get("indexPrice") or 0)
        prem = (fair - idx) / idx * 100 if idx else None
        return prem, price
    except Exception:
        return None, None


def get_price(coin):
    """Last price for a coin. MEXC first (works from GitHub Actions), Bybit fallback."""
    try:
        j = requests.get(f"https://contract.mexc.com/api/v1/contract/ticker",
                         params={"symbol": f"{coin}_USDT"}, timeout=8).json()
        return float(j["data"]["lastPrice"])
    except Exception:
        pass
    try:
        j = requests.get("https://api.bybit.com/v5/market/tickers",
                         params={"category": "linear", "symbol": f"{coin}USDT"}, timeout=8).json()
        return float(j["result"]["list"][0]["lastPrice"])
    except Exception:
        return None


def trade_cmd(coin, entry, risk=5):
    """Ready-to-paste prepare_trade command for the alert. User edits --entry
    to their actual level (rejection/bounce) after the chart read, then runs."""
    if not entry:
        return ""
    return ("\n\U0001f4cb <b>Ready-to-trade</b> (edit --entry to your level, then CONFIRM):\n"
            f"<code>cd ~/code/funding-alert &amp;&amp; python3 prepare_trade.py {coin} "
            f"--entry {entry:.6g} --risk {risk} --execute</code>")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"scan": {}, "watch": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_browser():
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        viewport={"width": 1920, "height": 1080},
    )
    return pw, browser, ctx


# ── Coinglass scrapers ───────────────────────────────────────────

def scrape_lowest_funding(page):
    """Extract 'Lowest Funding Rate' box from main page."""
    page.goto("https://www.coinglass.com/FundingRate", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(5000)

    text = page.evaluate("""() => {
        const body = document.body.innerText;
        const idx = body.indexOf('Lowest Funding Rate');
        if (idx === -1) return '';
        return body.substring(idx, idx + 400);
    }""")

    results = []
    lines = text.split("\n")
    i = 1  # skip the "Lowest Funding Rate" header
    while i < len(lines) - 1:
        line = lines[i].strip()
        next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
        rate_match = re.match(r"^(-?\d+\.?\d*)%$", next_line)
        if rate_match and ("/" in line):
            parts = line.split(" ", 1)
            if len(parts) == 2:
                exchange = parts[0]
                pair = parts[1]
                coin = pair.split("/")[0]
                rate = float(rate_match.group(1))
                results.append({
                    "coin": coin,
                    "exchange": exchange,
                    "pair": pair,
                    "rate": rate,
                })
            i += 2
        else:
            i += 1

    return results


def scrape_coin_funding(page, coin):
    """Scrape per-coin funding page for all exchanges."""
    page.goto(f"https://www.coinglass.com/funding/{coin}", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(4000)

    rows = page.evaluate("""() => {
        const rows = document.querySelectorAll('tr');
        const out = [];
        for (const row of rows) {
            const tds = row.querySelectorAll('td');
            if (tds.length < 5) continue;
            out.push(Array.from(tds).map(td => td.innerText.replace(/\\n/g, ' ').trim()));
        }
        return out;
    }""")

    results = []
    for row in rows:
        if len(row) < 6:
            continue
        exchange = row[0].strip()
        if not exchange or exchange == "Exchanges":
            continue
        pair = row[1].strip()
        rate_str = row[2].strip().replace("%", "")
        interval = row[4].strip()
        try:
            rate = float(rate_str)
            results.append({
                "coin": coin,
                "exchange": exchange,
                "pair": pair,
                "rate": rate,
                "interval": interval,
            })
        except ValueError:
            continue

    return results


def firecrawl_lowest_funding():
    """Scrape the 'Lowest Funding Rate' box via Firecrawl REST API — no browser.
    Returns the same shape as scrape_lowest_funding(), or None to trigger the
    Playwright fallback (missing key, request error, or unparseable page)."""
    key = os.environ.get("FIRECRAWL_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={"Authorization": f"Bearer {key}"},
            json={"url": "https://www.coinglass.com/FundingRate",
                  "formats": ["markdown"], "onlyMainContent": True,
                  "waitFor": 8000, "proxy": "auto", "maxAge": 0},   # maxAge 0 = always FRESH (no stale cache)
            timeout=60,
        )
        j = r.json()
        md = (j.get("data") or j).get("markdown", "") or ""
    except Exception as e:
        log(f"Firecrawl error: {str(e)[:70]}")
        return None
    if "Lowest Funding Rate" not in md:
        return None
    section = md[md.find("Lowest Funding Rate"):]
    end = section.find("USDT or USD")        # box ends right before the table
    if end > 0:
        section = section[:end]
    results = []
    for m in re.finditer(r"\[(\w+)\s+([A-Za-z0-9]+)/USDT[\\\s]*?(-?\d+\.\d+)%\]", section):
        results.append({"coin": m.group(2), "exchange": m.group(1),
                        "pair": f"{m.group(2)}/USDT", "rate": float(m.group(3))})
    return results or None


# ── Entry-data enrichment (order flow + heatmap deep-links) ──────

def enrich(coin):
    """Pull MEXC order-flow data + build Coinglass/TV deep-links for a flagged coin.
    MEXC works from GitHub Actions (Binance/Bybit are geo-blocked there).
    Every part is wrapped so a missing coin still yields the deep-links."""
    sym = f"{coin}_USDT"
    price = chg = vol_usd = oi_usd = premium = funding = None
    try:
        t = requests.get("https://contract.mexc.com/api/v1/contract/ticker",
                         params={"symbol": sym}, timeout=8).json().get("data", {})
        if t:
            price = float(t.get("lastPrice") or 0)
            chg = float(t.get("riseFallRate") or 0) * 100
            vol_usd = float(t.get("amount24") or 0)
            oi_usd = float(t.get("holdVol") or 0) * price
            fair = float(t.get("fairPrice") or 0)
            iprice = float(t.get("indexPrice") or 0)
            if iprice:
                premium = (fair - iprice) / iprice * 100   # real-time perp-vs-spot pressure
            funding = float(t.get("fundingRate") or 0) * 100
    except Exception:
        pass

    pump = None
    try:
        now = int(time.time())
        k = requests.get(f"https://contract.mexc.com/api/v1/contract/kline/{sym}",
                         params={"interval": "Min60", "start": now - 7*86400, "end": now},
                         timeout=8).json().get("data", {})
        lows, closes = k.get("low", []), k.get("close", [])
        if lows and closes:
            pump = (closes[-1] - min(lows)) / min(lows) * 100
    except Exception:
        pass

    walls = []
    try:
        d = requests.get(f"https://contract.mexc.com/api/v1/contract/depth/{sym}",
                         params={"limit": 50}, timeout=8).json().get("data", {})
        asks, bids = d.get("asks", []), d.get("bids", [])
        if asks and bids:
            mid = (asks[0][0] + bids[0][0]) / 2
            top = sorted(asks, key=lambda x: -x[1])[:3]
            top.sort(key=lambda x: x[0])
            for a in top:
                pct = (a[0] - mid) / mid * 100
                walls.append(f"     +{pct:.1f}%  ${a[1]*a[0]/1000:.0f}k @ {a[0]:.6g}")
    except Exception:
        pass

    b = ["", "\U0001f4ca <b>Entry data</b> (MEXC)"]
    if pump is not None:
        chg_s = f" | 24h {chg:+.0f}%" if chg is not None else ""
        b.append(f"  Pumped <b>+{pump:.0f}%</b> from 7d base{chg_s}")
    if oi_usd:
        b.append(f"  OI ${oi_usd/1e6:.1f}M | Vol ${vol_usd/1e6:.1f}M")
    if premium is not None:
        b.append(f"  Premium <b>{premium:+.2f}%</b> | funding {funding:+.3f}% (MEXC live)")
    if walls:
        b.append("  Ask walls (sweep targets):")
        b += walls
    b.append(
        f'  <a href="https://www.coinglass.com/pro/futures/LiquidationHeatMapNew?coin={coin}&type=pair">Liq heatmap</a>'
        f' · <a href="https://www.coinglass.com/pro/depth-delta?symbol=MEXC_{coin}USDT">Order book</a>'
        f' · <a href="https://www.coinglass.com/funding/{coin}">Funding (all CEX)</a>'
        f' · <a href="https://www.tradingview.com/chart/?symbol=MEXC:{coin}USDT.P">Chart</a>'
    )
    return "\n".join(b)


# ── Main ─────────────────────────────────────────────────────────

def scan():
    now = datetime.now(timezone.utc)
    log("Scan starting")

    watchlist = load_watchlist()
    log(f"Watchlist: {watchlist or 'empty'}")

    # ── MODE 1: lowest-funding box — Firecrawl first (no browser), Playwright fallback ──
    lowest = firecrawl_lowest_funding()
    src = "Firecrawl" if lowest is not None else None
    pw = browser = ctx = page = None
    if lowest is None or watchlist:           # browser only if FC failed or watchlist needs per-coin pages
        pw, browser, ctx = get_browser()
        page = ctx.new_page()
        if lowest is None:
            lowest = scrape_lowest_funding(page)
            src = "Playwright fallback"
    lowest = lowest or []

    try:
        log(f"Lowest funding: {len(lowest)} entries via {src}")
        for item in lowest:
            log(f"  {item['coin']} {item['exchange']}: {item['rate']}%")

        state = load_state()
        prev_scan = state.get("scan", {})
        prev_watch = state.get("watch", {})
        messages = []

        in_zone = [r for r in lowest if SCAN_LOW <= r["rate"] <= SCAN_HIGH]
        current_scan_keys = {f"{r['exchange']}:{r['coin']}" for r in in_zone}

        scan_new = [r for r in in_zone if f"{r['exchange']}:{r['coin']}" not in prev_scan]
        scan_gone = []
        for key in prev_scan:
            if key not in current_scan_keys:
                exchange, coin = key.split(":", 1)
                match = next((r for r in lowest if r["exchange"] == exchange and r["coin"] == coin), None)
                rate_now = match["rate"] if match else 0
                direction = "deeper" if rate_now < SCAN_LOW else "recovered"
                scan_gone.append({"coin": coin, "exchange": exchange, "rate": rate_now, "direction": direction})

        if scan_new or scan_gone:
            parts = []
            scan_btns = []
            if scan_new:
                parts.append("\U0001f534 <b>Entered -1.5% to -2.5% zone</b>")
                for a in scan_new:
                    parts.append(f"  <b>{a['coin']}</b> {a['exchange']}  <b>{a['rate']:.2f}%</b>")
                for coin in dict.fromkeys(a["coin"] for a in scan_new):
                    parts.append(enrich(coin))
                    parts.append(trade_cmd(coin, get_price(coin)))
                    # one-tap Short (all-auto: market, structural SL, base TP, $5)
                    scan_btns.append([{"text": f"\U0001f4c9 Short {coin} (confirm next)",
                                       "callback_data": f"t:{coin}"}])
            if scan_gone:
                for g in scan_gone:
                    emoji = "\U0001f53b" if g["direction"] == "deeper" else "✅"
                    parts.append(f"{emoji} <b>{g['coin']}</b> {g['exchange']} left zone ({g['direction']}, now {g['rate']:.2f}%)")
            parts.append(f"\n\U0001f4ca {now.strftime('%H:%M UTC')}")
            # Send scan alerts standalone so the Short buttons attach; else batch.
            if scan_btns:
                send_telegram("\n".join(parts), buttons=scan_btns)
            else:
                messages.append("\n".join(parts))

        state["scan"] = {f"{r['exchange']}:{r['coin']}": r["rate"] for r in in_zone}

        # ── MODE 2: Watchlist — full exchange breakdown ──────────
        if watchlist:
            all_watch = []
            for coin in watchlist:
                coin_data = scrape_coin_funding(page, coin)
                all_watch.extend(coin_data)
                log(f"  {coin}: {len(coin_data)} exchanges")

            watch_alerts = []
            for item in all_watch:
                if item["rate"] > -0.5:
                    continue
                key = f"{item['exchange']}:{item['coin']}"
                prev_rate = prev_watch.get(key)
                if prev_rate is None:
                    watch_alerts.append({**item, "change": "new"})
                elif abs(item["rate"] - prev_rate) >= WATCH_DELTA:
                    watch_alerts.append({**item, "prev_rate": prev_rate, "change": "moved"})

            if watch_alerts:
                parts = ["\U0001f50d <b>Watchlist Update</b>"]
                for a in sorted(watch_alerts, key=lambda x: x["rate"]):
                    interval = a.get("interval", "")
                    if a["change"] == "new":
                        parts.append(f"  <b>{a['coin']}</b> {a['exchange']}  <b>{a['rate']:.4f}%</b> / {interval}")
                    else:
                        arrow = "⬇️" if a["rate"] < a["prev_rate"] else "⬆️"
                        parts.append(f"  {arrow} <b>{a['coin']}</b> {a['exchange']}  {a['prev_rate']:.4f}% → <b>{a['rate']:.4f}%</b> / {interval}")
                for coin in dict.fromkeys(a["coin"] for a in watch_alerts):
                    parts.append(enrich(coin))
                parts.append(f"\n⏰ {now.strftime('%H:%M UTC')}")
                messages.append("\n".join(parts))

            state["watch"] = {f"{r['exchange']}:{r['coin']}": r["rate"] for r in all_watch}

        # ── MODE 3: Manual price-level alerts (active trade setups) ──
        price_alerts = load_price_alerts()
        fired = state.get("price_fired", [])
        for a in price_alerts:
            key = f"{a['coin']}:{a['level']}:{a['direction']}"
            if key in fired:
                continue
            px = get_price(a["coin"])
            if px is None:
                continue
            hit = ((a["direction"] == "above" and px >= a["level"]) or
                   (a["direction"] == "below" and px <= a["level"]))
            if hit:
                msg = (f"\U0001f3af <b>{a['coin']} hit {a['level']}</b>  (now {px:.5g})\n"
                       f"  {a.get('note', '')}")
                ctx = enrich(a["coin"])
                if ctx:
                    msg += "\n" + ctx
                # If the alert carries a full trade setup, attach a one-tap Short
                # button (tgbot shows the ticket to Confirm). Else fall back to the
                # copy-paste command and batch it with the other messages.
                if a.get("sl") and a.get("tp"):
                    risk = a.get("risk", 5)
                    cb = f"t:{a['coin']}:{a['sl']:g}:{a['tp']:g}:{risk:g}"
                    btn = [[{"text": f"\U0001f4c9 Short {a['coin']} (confirm next)", "callback_data": cb}]]
                    send_telegram(msg, buttons=btn)
                else:
                    msg += trade_cmd(a["coin"], a["level"])
                    messages.append(msg)
                fired.append(key)
                log(f"Price alert fired: {key} at {px}")
        state["price_fired"] = fired

        # ── MODE 5: Open-position exit monitor (watch coins you're IN) ──
        # Pings when an exit signal fires: premium flips flat (cover signal),
        # price reaches TP, or price nears the stop. MEXC data (works in cloud).
        # Refresh positions.json from the repo first — tgbot pushes trades there.
        try:
            import subprocess
            subprocess.run(["git", "-C", SCRIPT_DIR, "fetch", "--quiet", "origin", "main"],
                           capture_output=True, timeout=20)
            subprocess.run(["git", "-C", SCRIPT_DIR, "checkout", "origin/main", "--",
                            "positions.json", "price_alerts.json"],
                           capture_output=True, timeout=20)
        except Exception as e:
            log(f"positions pull skipped: {str(e)[:50]}")
        positions = load_positions()
        pos_state = state.get("pos", {})
        for p in positions:
            coin = p["coin"]
            ps = pos_state.get(coin, {})
            prem, px = mexc_premium_price(coin)
            if px is None:
                continue
            entry, sl, tp = p.get("entry"), p.get("sl"), p.get("tp")
            pnl = ((entry - px) / entry * 100) if entry else 0   # short PnL
            alerts = []
            # 1) premium flipped flat after being deep = selling done -> cover
            if prem is not None:
                if prem <= -1.5:
                    ps["was_deep"] = True
                if ps.get("was_deep") and prem >= -0.3 and not ps.get("cover_fired"):
                    alerts.append(f"\U0001f7e2 <b>COVER signal</b> — premium flipped flat ({prem:+.2f}%). Selling looks done.")
                    ps["cover_fired"] = True
            # 2) reached TP
            if tp and px <= tp and not ps.get("tp_fired"):
                alerts.append(f"\U0001f3af <b>TP hit</b> ({tp}). Take profit.")
                ps["tp_fired"] = True
            # 3) nearing stop (within 2%)
            if sl and px >= sl * 0.98 and not ps.get("sl_fired"):
                alerts.append(f"⚠️ <b>Nearing STOP</b> ({sl}). Squeeze risk.")
                ps["sl_fired"] = True
            if alerts:
                head = f"\U0001f4c2 <b>{coin}</b> {p.get('side','short')} | now {px:.5g} | PnL {pnl:+.1f}%"
                pm = f"  premium {prem:+.2f}% (MEXC)" if prem is not None else ""
                messages.append(head + "\n" + pm + "\n" + "\n".join("  " + a for a in alerts))
                log(f"Position alert {coin}: {[a[:30] for a in alerts]}")
            pos_state[coin] = ps
        # drop state for coins no longer held
        state["pos"] = {c: pos_state[c] for c in pos_state if any(p["coin"] == c for p in positions)}

        # ── Send ─────────────────────────────────────────────────
        for msg in messages:
            if len(msg) > 4000:
                msg = msg[:3950] + "\n... (truncated)"
            if send_telegram(msg):
                log(f"Alert sent ({len(msg)} chars)")
            else:
                log("Failed to send alert")

        if not messages:
            log(f"No changes. {len(in_zone)} in zone, {len(watchlist)} watched.")

        state["last_run"] = now.isoformat()
        save_state(state)
        log("Scan complete")

    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()


if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: BOT_TOKEN and CHAT_ID env vars are required (set them as GitHub Secrets)")
        sys.exit(1)

    if "--once" in sys.argv:
        scan()

    elif "--loop" in sys.argv:
        # Duration-capped loop for GitHub Actions (job max 6h). Scans every 15min
        # for ~5.8h then exits; the workflow's concurrency restart keeps it continuous.
        # No startup message here — avoids spam on every restart.
        end = time.time() + 5.8 * 3600
        log("Loop mode: scanning every 15min for ~5.8h, then exit for restart")
        while time.time() < end:
            try:
                scan()
            except Exception as e:
                log(f"Scan crashed: {e}")
            if time.time() < end:
                time.sleep(900)
        log("Loop window complete; exiting for workflow restart")

    else:
        # Local persistent loop (manual runs on your Mac)
        log("Funding Alert Bot started (local 15min loop)")
        send_telegram("✅ <b>Funding Alert Bot started</b>\nScanning Coinglass every 15min")
        while True:
            try:
                scan()
            except Exception as e:
                log(f"Scan crashed: {e}")
            time.sleep(900)
