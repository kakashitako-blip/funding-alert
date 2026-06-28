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

def ticket(coin, risk):
    cur = PT.bybit_price(coin)
    tp = PT.pre_pump_base(coin)
    o = PT.build(coin, cur, DEFAULT_SL_PCT, tp, risk, LEV)
    return cur, o

def place(coin, risk):
    cur, o = ticket(coin, risk)
    if o["risk"] > MAX_RISK: return None, f"risk ${o['risk']:.0f} > cap ${MAX_RISK:.0f}"
    sym = f"{coin}/USDT:USDT"; ex.load_markets()
    qty = ex.amount_to_precision(sym, o["qty"])
    sl = ex.price_to_precision(sym, o["sl"]); tp = ex.price_to_precision(sym, o["tp"])
    try: ex.set_leverage(LEV, sym)
    except Exception: pass
    order = ex.create_order(sym, "market", "sell", float(qty), None,
                            params={"stopLoss": sl, "takeProfit": tp, "positionIdx": 0})
    pos = [p for p in load_pos() if p["coin"] != coin]
    pos.append({"coin": coin, "side": "short", "entry": cur, "sl": o["sl"], "tp": o["tp"]})
    save_pos(pos)
    return order, None

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
        risk = float(pp[2]) if len(pp) > 2 else DEFAULT_RISK
        try:
            cur, o = ticket(coin, risk)
        except Exception as e:
            return send(f"✗ {coin}: {str(e)[:60]}")
        msg = (f"<b>SHORT {coin}</b> @ market ~{cur:.6g}\n"
               f"SL {o['sl']:.6g} (+{o['sl_pct']:.0f}%) · TP {o['tp']:.6g} · R {o['rr']:.2f}\n"
               f"${o['notional']:.0f} notional · risk ${o['risk']:.0f}")
        send(msg, kb_confirm(f"go:{coin}:{risk}"))
    elif c in ("/close", "/c") and len(pp) >= 2:
        coin = pp[1].upper().replace("USDT", "")
        send(f"Close <b>{coin}</b>?", kb_confirm(f"cl:{coin}"))
    elif c in ("/status", "/st"):
        send(status())
    else:
        send("/short COIN [risk] · /close COIN · /status")

def handle_cb(data, cid):
    if data == "x":
        return answer(cid, "Cancelled")
    if data.startswith("go:"):
        _, coin, risk = data.split(":")
        answer(cid, "Placing…")
        try:
            order, err = place(coin, float(risk))
            send(f"✗ {err}" if err else f"✅ SHORT {coin} filled (id {order.get('id')}). Now monitored for exit.")
        except Exception as e:
            send(f"✗ order rejected: {str(e)[:80]}")
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
