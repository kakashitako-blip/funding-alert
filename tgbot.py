#!/usr/bin/env python3
"""
Telegram TRADING bot — take/close trades from Telegram instead of the terminal.

Runs on YOUR Mac (Bybit is geo-blocked from the cloud). Long-polls Telegram,
locked to YOUR chat id. Every order has a Confirm button. Integrates with
positions.json so the cloud monitor watches exits automatically.

  python3 tgbot.py     # leave it running; trade from Telegram

Commands (send in Telegram):
  /short COIN [risk]   — market short, tight stop, base TP  (asks to Confirm)
  /close COIN          — close the position                 (asks to Confirm)
  /status              — open positions + live PnL
  /help
"""
import os, time, json, subprocess, requests
import prepare_trade as PT          # reuse bybit_price, pre_pump_base, build

TOKEN = os.environ.get("BOT_TOKEN"); CHAT = str(os.environ.get("CHAT_ID", ""))
KEY = os.environ.get("BYBIT_API_KEY"); SEC = os.environ.get("BYBIT_API_SECRET")
API = f"https://api.telegram.org/bot{TOKEN}"
DEFAULT_RISK, DEFAULT_SL_PCT, LEV, MAX_RISK = 5.0, 6.0, 4, 50.0
HERE = os.path.dirname(os.path.abspath(__file__))
POS = os.path.join(HERE, "positions.json")

import ccxt
ex = ccxt.bybit({"apiKey": KEY, "secret": SEC, "options": {"defaultType": "linear"}})

def send(text, kb=None):
    d = {"chat_id": CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if kb: d["reply_markup"] = json.dumps(kb)
    requests.post(f"{API}/sendMessage", json=d, timeout=10)

def answer(cid, text=""):
    requests.post(f"{API}/answerCallbackQuery", json={"callback_query_id": cid, "text": text}, timeout=10)

def load_pos():
    try: return json.load(open(POS))
    except Exception: return []
def save_pos(p):
    json.dump(p, open(POS, "w"), indent=2)
    _git_sync()

def _git_sync():
    """Commit + push positions.json so the cloud monitor stays in sync."""
    try:
        subprocess.run(["git", "-C", HERE, "add", "positions.json"], capture_output=True, timeout=20)
        subprocess.run(["git", "-C", HERE, "commit", "-m", "tgbot: sync positions"], capture_output=True, timeout=20)
        subprocess.run(["git", "-C", HERE, "push"], capture_output=True, timeout=30)
    except Exception as e:
        print("git sync:", str(e)[:60])

pending = {}  # coin -> built order awaiting Confirm

def parse_kv(tokens):
    """entry=, sl=, tp= (absolute prices) · risk= ($). Bare number = risk."""
    d = {}
    for tok in tokens:
        if "=" in tok:
            k, v = tok.split("=", 1)
            try: d[k.lower()] = float(v)
            except Exception: pass
        else:
            try: d["risk"] = float(tok)
            except Exception: pass
    return d

def build_order(coin, args):
    cur = PT.bybit_price(coin)
    market = "entry" not in args
    entry = args.get("entry", cur)
    sl = args.get("sl")
    if sl is None:                                # structural: just above the swept high
        rh = PT.recent_high(coin)
        sl = (rh * 1.02) if rh else entry * (1 + DEFAULT_SL_PCT / 100)
    tp = args.get("tp")
    if tp is None:
        tp = PT.pre_pump_base(coin)
    risk = min(args.get("risk", DEFAULT_RISK), MAX_RISK)
    if sl <= entry:
        raise ValueError("SL must be ABOVE entry for a short")
    if not tp or tp >= entry:
        raise ValueError("TP must be BELOW entry — pass tp=")
    risk_frac = (sl - entry) / entry
    qty = risk / (sl - entry)
    return {"coin": coin, "cur": cur, "market": market, "entry": entry, "sl": sl,
            "tp": tp, "risk": risk, "qty": qty, "notional": qty * entry,
            "rr": ((entry - tp) / entry) / risk_frac, "sl_pct": risk_frac * 100}

def place_order(o):
    coin = o["coin"]; sym = f"{coin}/USDT:USDT"; ex.load_markets()
    qty = ex.amount_to_precision(sym, o["qty"])
    sl = ex.price_to_precision(sym, o["sl"]); tp = ex.price_to_precision(sym, o["tp"])
    try: ex.set_leverage(LEV, sym)
    except Exception: pass
    if o["market"]:
        order = ex.create_order(sym, "market", "sell", float(qty), None,
                                params={"stopLoss": sl, "takeProfit": tp, "positionIdx": 0})
        entry_rec = o["cur"]
    else:
        price = ex.price_to_precision(sym, o["entry"])
        order = ex.create_order(sym, "limit", "sell", float(qty), float(price),
                                params={"stopLoss": sl, "takeProfit": tp, "positionIdx": 0})
        entry_rec = o["entry"]
    pos = [p for p in load_pos() if p["coin"] != coin]
    pos.append({"coin": coin, "side": "short", "entry": entry_rec, "sl": o["sl"], "tp": o["tp"]})
    save_pos(pos)
    return order

def close(coin):
    sym = f"{coin}/USDT:USDT"
    p = ex.fetch_position(sym)
    if not p.get("contracts"): return None, "no open position"
    order = ex.create_order(sym, "market", "buy", p["contracts"], params={"reduceOnly": True})
    save_pos([x for x in load_pos() if x["coin"] != coin])
    return p, order

def status():
    try:
        ps = [p for p in ex.fetch_positions(params={"settleCoin": "USDT"}) if p.get("contracts")]
    except Exception as e:
        return f"status error: {str(e)[:60]}"
    if not ps: return "No open positions."
    out = ["<b>Open positions</b>"]
    for p in ps:
        coin = p["symbol"].split("/")[0]
        upnl = float(p.get("unrealizedPnl") or 0)
        im = float(p.get("initialMargin") or p.get("collateral") or 0)
        entry = float(p.get("entryPrice") or 0)
        mark = float(p.get("markPrice") or 0)
        roi = (upnl / im * 100) if im else 0
        emoji = "🟢" if upnl >= 0 else "🔴"
        out.append(f"  {emoji} {coin} {p['side']} | {upnl:+.2f} USDT ({roi:+.1f}% ROI) | entry {entry:.6g} mark {mark:.6g}")
    return "\n".join(out)

def kb_confirm(action): return {"inline_keyboard": [[
    {"text": "✅ Confirm", "callback_data": action}, {"text": "❌ Cancel", "callback_data": "x"}]]}

def handle(text):
    pp = text.strip().split()
    if not pp: return
    c = pp[0].lower()
    if c in ("/short", "/s") and len(pp) >= 2:
        coin = pp[1].upper().replace("USDT", "")
        try:
            o = build_order(coin, parse_kv(pp[2:]))
        except Exception as e:
            return send(f"✗ {coin}: {str(e)[:90]}")
        pending[coin] = o
        kind = "MARKET (fills now)" if o["market"] else f"LIMIT @ {o['entry']:.6g}"
        msg = (f"<b>SHORT {coin}</b> · {kind}\n"
               f"entry {o['entry']:.6g} · SL {o['sl']:.6g} (+{o['sl_pct']:.1f}%) · TP {o['tp']:.6g}\n"
               f"R {o['rr']:.2f} · {o['qty']:.4g} {coin} (${o['notional']:.0f}) · risk ${o['risk']:.0f}")
        send(msg, kb_confirm(f"go:{coin}"))
    elif c in ("/close", "/c") and len(pp) >= 2:
        coin = pp[1].upper().replace("USDT", "")
        send(f"Close <b>{coin}</b>?", kb_confirm(f"cl:{coin}"))
    elif c in ("/status", "/st"):
        send(status())
    else:
        send("<b>/short COIN</b> [entry= sl= tp= risk=]\n"
             "  e.g. <code>/short ARK</code> (all auto, market)\n"
             "  <code>/short ARK risk=8</code>\n"
             "  <code>/short ARK entry=0.14 sl=0.168 tp=0.10 risk=5</code>\n"
             "  (entry omitted = market · sl/tp = prices · default sl +6%, tp = base)\n"
             "<b>/close COIN</b> · <b>/status</b>")

def handle_cb(data, cid):
    if data == "x":
        return answer(cid, "Cancelled")
    if data.startswith("go:"):
        coin = data.split(":")[1]
        o = pending.get(coin)
        answer(cid, "Placing…" if o else "Expired — re-issue")
        if not o:
            return
        try:
            order = place_order(o)
            kind = "filled" if o["market"] else "limit placed"
            send(f"✅ SHORT {coin} {kind} (id {order.get('id')}). Now monitored for exit.")
            pending.pop(coin, None)
        except Exception as e:
            send(f"✗ order rejected: {str(e)[:90]}")
    elif data.startswith("cl:"):
        coin = data.split(":")[1]
        answer(cid, "Closing…")
        try:
            p, _ = close(coin)
            send(f"✅ Closed {coin}." if p else f"No {coin} position.")
        except Exception as e:
            send(f"✗ {str(e)[:80]}")

def main():
    if not all([TOKEN, CHAT, KEY, SEC]):
        print("Set BOT_TOKEN, CHAT_ID, BYBIT_API_KEY, BYBIT_API_SECRET"); return
    send("🤖 <b>Trading bot online</b>\n/short COIN [risk] · /close COIN · /status")
    offset = None
    while True:
        try:
            r = requests.get(f"{API}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=35).json()
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u and str(u["message"]["chat"]["id"]) == CHAT:
                    handle(u["message"].get("text", ""))
                elif "callback_query" in u and str(u["callback_query"]["message"]["chat"]["id"]) == CHAT:
                    handle_cb(u["callback_query"]["data"], u["callback_query"]["id"])
        except Exception as e:
            print("loop err:", str(e)[:80]); time.sleep(3)

if __name__ == "__main__":
    main()
