"""
notifier.py - Telegram notifications for PowerTrader.

Settings live in telegram_settings.json next to this file (written by the Hub's
Settings -> Telegram wizard):
  {"enabled": true, "token": "123:ABC...", "chat_id": "123456789",
   "notify_trades": true, "notify_alerts": true}
Env vars TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID override the file.

Nothing here ever raises into the trader: a failed notification is silently dropped.
"""

import json
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(BASE_DIR, "telegram_settings.json")

DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "token": "",
    "chat_id": "",
    "notify_trades": True,
    "notify_alerts": True,
}

_cache: Dict[str, Any] = {"mtime": None, "data": dict(DEFAULTS)}
_last_alert: Dict[str, float] = {}
_active_alerts: set = set()


def load_settings() -> Dict[str, Any]:
    try:
        mtime = os.path.getmtime(SETTINGS_PATH) if os.path.isfile(SETTINGS_PATH) else None
    except Exception:
        mtime = None

    if mtime != _cache["mtime"]:
        data = dict(DEFAULTS)
        if mtime is not None:
            try:
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    data.update({k: raw[k] for k in DEFAULTS if k in raw})
            except Exception:
                pass
        _cache["mtime"] = mtime
        _cache["data"] = data

    out = dict(_cache["data"])
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        out["token"] = os.environ["TELEGRAM_BOT_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID"):
        out["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    out["token"] = str(out.get("token") or "").strip()
    out["chat_id"] = str(out.get("chat_id") or "").strip()
    return out


def save_settings(settings: Dict[str, Any]) -> None:
    clean = {
        "enabled": bool(settings.get("enabled", False)),
        "token": str(settings.get("token") or "").strip(),
        "chat_id": str(settings.get("chat_id") or "").strip(),
        "notify_trades": bool(settings.get("notify_trades", True)),
        "notify_alerts": bool(settings.get("notify_alerts", True)),
    }
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    _cache["mtime"] = None  # force reload


def send_sync(token: str, chat_id: str, text: str) -> Tuple[bool, str]:
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"},
            timeout=10,
        )
        if r.status_code == 200 and r.json().get("ok"):
            return True, "Message sent."
        try:
            desc = r.json().get("description", r.text[:200])
        except Exception:
            desc = r.text[:200]
        return False, f"Telegram error {r.status_code}: {desc}"
    except Exception as e:
        return False, f"Could not reach Telegram: {e}"


def detect_chat_id(token: str) -> Tuple[Optional[str], str]:
    """Finds the chat id of whoever last messaged the bot."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
        data = r.json()
        if not data.get("ok"):
            return None, f"Telegram error: {data.get('description', r.text[:200])}"
        for upd in reversed(data.get("result", [])):
            msg = upd.get("message") or upd.get("channel_post") or upd.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"]), "Found."
        return None, "No messages found. Send any message to your bot in Telegram first, then try again."
    except Exception as e:
        return None, f"Could not reach Telegram: {e}"


def _mode_prefix() -> str:
    try:
        import okx_client
        return "[DEMO] " if okx_client.load_settings().get("demo", True) else ""
    except Exception:
        return ""


def _send_async(text: str, kind: str) -> None:
    s = load_settings()
    if not s["enabled"] or not s["token"] or not s["chat_id"]:
        return
    if kind == "trade" and not s["notify_trades"]:
        return
    if kind == "alert" and not s["notify_alerts"]:
        return
    msg = _mode_prefix() + text
    threading.Thread(target=send_sync, args=(s["token"], s["chat_id"], msg), daemon=True).start()


def _px(p: Any) -> str:
    try:
        p = float(p)
        return f"${p:,.2f}" if p >= 1 else f"${p:.6g}"
    except Exception:
        return "N/A"


def _money(v: Any) -> str:
    try:
        v = float(v)
        return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"
    except Exception:
        return "N/A"


def notify_trade(entry: Dict[str, Any], total_realized: Optional[float] = None) -> None:
    """Called by the trader after every recorded fill."""
    try:
        side = str(entry.get("side", "")).lower()
        tag = str(entry.get("tag", "") or "").upper()
        base = str(entry.get("symbol", "")).split("-")[0]
        qty = entry.get("qty")
        try:
            qty_txt = f"{float(qty):.8g}"
        except Exception:
            qty_txt = str(qty)

        lines = []
        if side == "buy":
            if tag == "LTH":
                title = f"🏦 LTH buy {base}"
            elif tag == "DCA":
                title = f"🟠 DCA buy {base}"
            else:
                title = f"🟢 Buy {base}"
            lines.append(title)
            lines.append(f"{qty_txt} @ {_px(entry.get('price'))}")
            if entry.get("notional_usd") is not None:
                lines.append(f"Spent: ${float(entry['notional_usd']):,.2f}")
            if tag == "DCA" and entry.get("pnl_pct") is not None:
                lines.append(f"Position was at {float(entry['pnl_pct']):+.2f}%")
        else:
            realized = entry.get("realized_profit_usd")
            icon = "✅" if (realized is None or float(realized) >= 0) else "🔻"
            lines.append(f"{icon} Sold {base}")
            lines.append(f"{qty_txt} @ {_px(entry.get('price'))}")
            if entry.get("pnl_pct") is not None:
                lines.append(f"Trade result: {float(entry['pnl_pct']):+.2f}%")
            if realized is not None:
                lines.append(f"Profit: {_money(realized)}")
            if total_realized is not None:
                lines.append(f"Total realized: {_money(total_realized)}")

        _send_async("\n".join(lines), "trade")
    except Exception:
        pass


def alert(key: str, text: str, cooldown: float = 1800.0) -> None:
    """Problem notification; the same key is sent at most once per `cooldown` seconds."""
    try:
        now = time.time()
        if now - _last_alert.get(key, 0.0) < float(cooldown):
            return
        _last_alert[key] = now
        _active_alerts.add(key)
        _send_async(text, "alert")
    except Exception:
        pass


def resolved(key: str, text: str) -> None:
    """Sends a recovery message, but only if `alert(key, ...)` was sent earlier."""
    try:
        if key in _active_alerts:
            _active_alerts.discard(key)
            _last_alert.pop(key, None)
            _send_async(text, "alert")
    except Exception:
        pass
