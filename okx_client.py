"""
okx_client.py - OKX spot trading adapter for PowerTrader.

Why this file exists
--------------------
The original bot talked to Robinhood's Crypto API and parsed Robinhood-shaped JSON
all over pt_trader.py. Instead of rewriting the strategy code, this module:

  1. Talks to the OKX v5 REST API (signed with API key + secret + passphrase).
  2. Translates every call the trader makes (account, holdings, orders, prices ...)
     into OKX requests and returns Robinhood-shaped JSON, so the strategy logic is untouched.

Symbols: the bot keeps using "BTC-USD" internally. They are mapped to "BTC-<QUOTE>" on OKX,
where QUOTE is configurable (default USDC; see okx_settings.json).

Credentials (first match wins):
  environment variables: OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE
  files next to this script: okx_key.txt, okx_secret.txt, okx_passphrase.txt

Settings (okx_settings.json next to this script, env vars override):
  {"demo": true, "quote": "USDC", "base_url": "https://eea.okx.com"}
  env: OKX_DEMO=1/0, OKX_QUOTE, OKX_BASE_URL

demo = true  -> sends the "x-simulated-trading: 1" header. Use API keys created in OKX
                *Demo Trading* mode. No real money is touched.
demo = false -> LIVE trading with real funds.
"""

import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse, parse_qs

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Hosts to try (EEA accounts such as Poland normally use eea.okx.com / my.okx.com).
KNOWN_HOSTS = ["https://eea.okx.com", "https://www.okx.com", "https://app.okx.com"]

DEFAULT_SETTINGS = {"demo": True, "quote": "USDC", "base_url": KNOWN_HOSTS[0]}

# Assets that are never treated as tradable "holdings" by the bot.
NON_TRADABLE = {"USDT", "USDC", "USD", "EUR", "DAI", "PYUSD", "USDG", "TUSD", "FDUSD"}

SETTINGS_PATH = os.path.join(BASE_DIR, "okx_settings.json")
CRED_FILES = {
    "key": os.path.join(BASE_DIR, "okx_key.txt"),
    "secret": os.path.join(BASE_DIR, "okx_secret.txt"),
    "passphrase": os.path.join(BASE_DIR, "okx_passphrase.txt"),
}


# ----------------------------------------------------------------------------
# settings / credentials
# ----------------------------------------------------------------------------
def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def load_settings() -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.isfile(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                s.update({k: v for k, v in data.items() if k in s})
    except Exception:
        pass

    env_demo = os.environ.get("OKX_DEMO")
    if env_demo is not None and env_demo.strip() != "":
        s["demo"] = env_demo.strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("OKX_QUOTE"):
        s["quote"] = os.environ["OKX_QUOTE"].strip().upper()
    if os.environ.get("OKX_BASE_URL"):
        s["base_url"] = os.environ["OKX_BASE_URL"].strip()

    s["quote"] = str(s.get("quote") or "USDC").upper().strip()
    s["base_url"] = str(s.get("base_url") or KNOWN_HOSTS[0]).rstrip("/")
    s["demo"] = bool(s.get("demo", True))
    return s


def save_settings(settings: Dict[str, Any]) -> None:
    clean = {
        "demo": bool(settings.get("demo", True)),
        "quote": str(settings.get("quote") or "USDC").upper().strip(),
        "base_url": str(settings.get("base_url") or KNOWN_HOSTS[0]).rstrip("/"),
    }
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)


def load_credentials() -> Optional[Tuple[str, str, str]]:
    key = os.environ.get("OKX_API_KEY", "").strip() or _read_text(CRED_FILES["key"])
    secret = os.environ.get("OKX_API_SECRET", "").strip() or _read_text(CRED_FILES["secret"])
    passphrase = os.environ.get("OKX_API_PASSPHRASE", "").strip() or _read_text(CRED_FILES["passphrase"])
    if key and secret and passphrase:
        return key, secret, passphrase
    return None


def save_credentials(key: str, secret: str, passphrase: str) -> None:
    for name, value in (("key", key), ("secret", secret), ("passphrase", passphrase)):
        with open(CRED_FILES[name], "w", encoding="utf-8") as f:
            f.write(value.strip())


def credentials_status() -> Tuple[bool, str]:
    missing = []
    if not (os.environ.get("OKX_API_KEY", "").strip() or _read_text(CRED_FILES["key"])):
        missing.append("okx_key.txt")
    if not (os.environ.get("OKX_API_SECRET", "").strip() or _read_text(CRED_FILES["secret"])):
        missing.append("okx_secret.txt")
    if not (os.environ.get("OKX_API_PASSPHRASE", "").strip() or _read_text(CRED_FILES["passphrase"])):
        missing.append("okx_passphrase.txt")
    if missing:
        return False, "missing " + ", ".join(missing)
    s = load_settings()
    mode = "DEMO (simulated funds)" if s["demo"] else "LIVE (real funds)"
    return True, f"{mode}, quote {s['quote']}, host {s['base_url']}"


# ----------------------------------------------------------------------------
# low-level OKX client
# ----------------------------------------------------------------------------
class OkxClient:
    def __init__(
        self,
        creds: Optional[Tuple[str, str, str]] = None,
        settings: Optional[Dict[str, Any]] = None,
        timeout: int = 10,
    ):
        self.settings = settings or load_settings()
        self.creds = creds if creds is not None else load_credentials()
        self.base_url = self.settings["base_url"]
        self.demo = bool(self.settings["demo"])
        self.timeout = timeout
        self.session = requests.Session()

    def _timestamp(self) -> str:
        now = datetime.datetime.now(datetime.timezone.utc)
        return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"

    def _headers(self, method: str, request_path: str, body: str, auth: bool) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.demo:
            headers["x-simulated-trading"] = "1"
        if auth:
            if not self.creds:
                raise RuntimeError("OKX credentials not found (okx_key.txt / okx_secret.txt / okx_passphrase.txt).")
            key, secret, passphrase = self.creds
            ts = self._timestamp()
            prehash = f"{ts}{method.upper()}{request_path}{body}"
            sig = base64.b64encode(
                hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
            ).decode("utf-8")
            headers.update({
                "OK-ACCESS-KEY": key,
                "OK-ACCESS-SIGN": sig,
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": passphrase,
            })
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
    ) -> Dict[str, Any]:
        """Returns OKX's JSON envelope {"code","msg","data"}. Never raises."""
        method = method.upper()
        request_path = path
        if params:
            clean = {k: v for k, v in params.items() if v is not None and v != ""}
            if clean:
                request_path = f"{path}?{urlencode(clean)}"
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        try:
            headers = self._headers(method, request_path, body_str, auth)
            url = self.base_url + request_path
            if method == "GET":
                resp = self.session.get(url, headers=headers, timeout=self.timeout)
            else:
                resp = self.session.request(method, url, headers=headers, data=body_str, timeout=self.timeout)
            try:
                data = resp.json()
            except Exception:
                return {"code": f"http{resp.status_code}", "msg": resp.text[:300], "data": []}
            if not isinstance(data, dict):
                return {"code": "-1", "msg": "unexpected response", "data": []}
            return data
        except Exception as e:
            return {"code": "-1", "msg": str(e), "data": []}

    # ---- public market data (no auth) ----
    def ticker(self, inst_id: str) -> Optional[Dict[str, Any]]:
        r = self.request("GET", "/api/v5/market/ticker", params={"instId": inst_id}, auth=False)
        if r.get("code") == "0" and r.get("data"):
            return r["data"][0]
        return None

    # ---- connection test used by the Hub wizard ----
    def test(self) -> Tuple[bool, str]:
        if not self.creds:
            return False, "Credentials missing."
        r = self.request("GET", "/api/v5/account/balance")
        code = str(r.get("code"))
        if code == "0":
            return True, "OK - authenticated."
        hints = {
            "50101": "API key does not match this environment. Check the Demo/Live setting (demo keys only work "
                     "with Demo on, live keys with Demo off) and the host (EEA accounts use eea.okx.com).",
            "50102": "Timestamp request expired - check your computer's clock.",
            "50103": "API key missing.",
            "50104": "Passphrase missing.",
            "50105": "Wrong passphrase.",
            "50111": "Invalid API key.",
            "50112": "Invalid timestamp.",
            "50113": "Invalid signature - the secret key is wrong.",
            "50119": "API key doesn't exist on this host - try another host, or check the key.",
            "50110": "Your IP is not on the API key's allowlist.",
        }
        return False, f"OKX error {code}: {r.get('msg')}\n{hints.get(code, '')}".strip()


# ----------------------------------------------------------------------------
# Robinhood-shaped shim used by pt_trader.py / pt_hub.py
# ----------------------------------------------------------------------------
def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except Exception:
        return default


class OkxRobinhoodShim:
    """request(method, path, body) accepts the Robinhood paths the bot uses and returns
    Robinhood-shaped dicts (or None on a transient failure, like the old code did)."""

    def __init__(self, client: Optional[OkxClient] = None):
        self.client = client or OkxClient()
        self.quote = self.client.settings["quote"]
        self._inst: Dict[str, Dict[str, Any]] = {}
        self._inst_ts = 0.0
        self._recent: Dict[str, str] = {}  # ordId -> instId (orders we placed this run)

    # ---- symbol / instrument helpers ----
    def to_inst(self, symbol: str) -> str:
        s = str(symbol or "").upper().strip()
        if s.endswith("-USD"):
            return s[:-4] + "-" + self.quote
        return s

    def to_bot_symbol(self, inst_id: str) -> str:
        base = str(inst_id).split("-")[0]
        return f"{base}-USD"

    def _load_instruments(self) -> Dict[str, Dict[str, Any]]:
        if self._inst and (time.time() - self._inst_ts) < 3600:
            return self._inst
        r = self.client.request("GET", "/api/v5/public/instruments", params={"instType": "SPOT"}, auth=False)
        if r.get("code") == "0":
            self._inst = {i["instId"]: i for i in r.get("data", []) if i.get("instId")}
            self._inst_ts = time.time()
        return self._inst

    # ---- entry point ----
    def request(self, method: str, path: str, body: str = "") -> Any:
        method = method.upper()
        u = urlparse(path)
        route = u.path.rstrip("/")
        q = {k: v[0] for k, v in parse_qs(u.query).items()}

        try:
            if route == "/api/v1/crypto/trading/accounts" and method == "GET":
                return self._accounts()
            if route == "/api/v1/crypto/trading/holdings" and method == "GET":
                return self._holdings()
            if route == "/api/v1/crypto/trading/trading_pairs" and method == "GET":
                return self._trading_pairs()
            if route == "/api/v1/crypto/trading/orders" and method == "GET":
                return self._orders(q.get("symbol", ""))
            if route == "/api/v1/crypto/trading/orders" and method == "POST":
                return self._place_order(json.loads(body or "{}"))
            if route == "/api/v1/crypto/marketdata/best_bid_ask" and method == "GET":
                return self._best_bid_ask(q.get("symbol", ""))
        except Exception:
            return None
        return None

    # ---- account ----
    def _balances(self, ccy: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        r = self.client.request("GET", "/api/v5/account/balance", params={"ccy": ccy})
        if r.get("code") != "0":
            return None
        data = r.get("data") or []
        if not data:
            return []
        return data[0].get("details") or []

    def _accounts(self) -> Optional[Dict[str, Any]]:
        details = self._balances(self.quote)
        if details is None:
            return None
        avail = 0.0
        for d in details:
            if str(d.get("ccy", "")).upper() == self.quote:
                avail = _f(d.get("availBal"))
        return {"account_number": "okx", "status": "active", "buying_power": f"{avail:.8f}",
                "buying_power_currency": self.quote}

    def _holdings(self) -> Optional[Dict[str, Any]]:
        details = self._balances()
        if details is None:
            return None
        inst = self._load_instruments()
        out = []
        for d in details:
            ccy = str(d.get("ccy", "")).upper().strip()
            if not ccy or ccy == self.quote or ccy in NON_TRADABLE:
                continue
            total = _f(d.get("cashBal"))
            if total <= 0.0:
                continue
            # skip coins we cannot price against our quote currency (dust, unlisted, etc.)
            if inst and f"{ccy}-{self.quote}" not in inst:
                continue
            out.append({
                "asset_code": ccy,
                "total_quantity": f"{total:.12f}".rstrip("0").rstrip("."),
                "quantity_available_for_trading": f"{_f(d.get('availBal')):.12f}".rstrip("0").rstrip("."),
            })
        return {"results": out, "next": None, "previous": None}

    def _trading_pairs(self) -> Optional[Dict[str, Any]]:
        inst = self._load_instruments()
        if not inst:
            return None
        res = []
        for inst_id, i in inst.items():
            if i.get("quoteCcy", "").upper() == self.quote and i.get("state") == "live":
                res.append({"symbol": f"{i.get('baseCcy', '').upper()}-USD", "status": "tradable",
                            "min_order_size": i.get("minSz"), "quantity_increment": i.get("lotSz")})
        return {"results": res, "next": None, "previous": None}

    # ---- prices ----
    def _best_bid_ask(self, symbol: str) -> Optional[Dict[str, Any]]:
        t = self.client.ticker(self.to_inst(symbol))
        if not t:
            return None
        ask, bid = _f(t.get("askPx")), _f(t.get("bidPx"))
        if ask <= 0.0 or bid <= 0.0:
            return None
        return {"results": [{
            "symbol": str(symbol).upper(),
            "price": _f(t.get("last")),
            "ask_inclusive_of_buy_spread": f"{ask}",
            "bid_inclusive_of_sell_spread": f"{bid}",
        }]}

    # ---- orders ----
    def _norm_order(self, o: Dict[str, Any]) -> Dict[str, Any]:
        inst_id = o.get("instId", "")
        base = inst_id.split("-")[0]
        side = str(o.get("side", "")).lower()
        st = str(o.get("state", "")).lower()
        state = {"filled": "filled", "canceled": "canceled", "mmp_canceled": "canceled",
                 "live": "open", "partially_filled": "partially_filled"}.get(st, st)

        acc = _f(o.get("accFillSz"))
        px = _f(o.get("avgPx"))
        fee = _f(o.get("fee"))  # negative number = fee charged
        fee_ccy = str(o.get("feeCcy", "")).upper()

        net_qty = acc
        fee_usd = 0.0
        if fee < 0:
            if fee_ccy == base:
                # OKX takes buy fees out of the coin received: real holding is smaller than accFillSz.
                if side == "buy":
                    net_qty = max(0.0, acc - abs(fee))
                fee_usd = abs(fee) * px
            elif fee_ccy == self.quote:
                fee_usd = abs(fee)

        try:
            created = datetime.datetime.fromtimestamp(int(o.get("cTime", 0)) / 1000.0, datetime.timezone.utc)
            created_at = created.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        except Exception:
            created_at = ""

        return {
            "id": o.get("ordId"),
            "client_order_id": o.get("clOrdId"),
            "symbol": self.to_bot_symbol(inst_id),
            "side": side,
            "type": str(o.get("ordType", "market")).lower(),
            "state": state,
            "created_at": created_at,
            "filled_asset_quantity": f"{net_qty:.12f}",
            "average_price": f"{px:.12f}" if px > 0 else None,
            "fee": f"{fee_usd:.8f}",
            "executions": ([{"quantity": f"{net_qty:.12f}", "effective_price": f"{px:.12f}"}]
                           if (net_qty > 0 and px > 0) else []),
        }

    def _orders(self, symbol: str) -> Optional[Dict[str, Any]]:
        inst_id = self.to_inst(symbol)
        if not inst_id:
            return None

        by_id: Dict[str, Dict[str, Any]] = {}
        ok_any = False

        # history (last 3 months), newest first, paged with `after`
        after = None
        for _ in range(5):
            r = self.client.request("GET", "/api/v5/trade/orders-history-archive",
                                    params={"instType": "SPOT", "instId": inst_id, "limit": "100", "after": after})
            if r.get("code") != "0":
                break
            ok_any = True
            rows = r.get("data") or []
            for o in rows:
                by_id[o["ordId"]] = o
            if len(rows) < 100:
                break
            after = rows[-1]["ordId"]

        # still-open orders
        r = self.client.request("GET", "/api/v5/trade/orders-pending",
                                params={"instType": "SPOT", "instId": inst_id})
        if r.get("code") == "0":
            ok_any = True
            for o in r.get("data") or []:
                by_id[o["ordId"]] = o

        # orders we just placed (history can lag by a moment)
        for ord_id, oi in list(self._recent.items()):
            if oi != inst_id or ord_id in by_id:
                continue
            r = self.client.request("GET", "/api/v5/trade/order", params={"instId": inst_id, "ordId": ord_id})
            if r.get("code") == "0" and r.get("data"):
                by_id[ord_id] = r["data"][0]

        if not ok_any and not by_id:
            return None

        results = [self._norm_order(o) for o in by_id.values()]
        results.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        return {"results": results, "next": None, "previous": None}

    def _place_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        side = str(body.get("side", "")).lower()
        inst_id = self.to_inst(body.get("symbol", ""))
        try:
            qty = Decimal(str((body.get("market_order_config") or {}).get("asset_quantity", "0")))
        except Exception:
            qty = Decimal("0")

        inst = self._load_instruments().get(inst_id)
        if not inst:
            return {"errors": [{"detail": f"{inst_id} is not a tradable OKX spot instrument", "code": "invalid_symbol"}]}

        lot = Decimal(str(inst.get("lotSz") or "0.00000001"))
        min_sz = Decimal(str(inst.get("minSz") or "0"))
        if lot > 0:
            qty = (qty // lot) * lot  # round DOWN to the lot size (never ask for more than we have)
        if qty <= 0 or qty < min_sz:
            return {"errors": [{"detail": f"quantity must be greater than or equal to {min_sz}", "code": "min_size"}]}

        cl = re.sub(r"[^A-Za-z0-9]", "", str(body.get("client_order_id") or uuid.uuid4().hex))[:32]
        payload = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": "market",
            "sz": format(qty.normalize(), "f"),
            "tgtCcy": "base_ccy",  # size is in the coin, not in quote currency
            "clOrdId": cl,
        }
        r = self.client.request("POST", "/api/v5/trade/order", body=payload)
        data = (r.get("data") or [{}])[0] if isinstance(r.get("data"), list) else {}
        if r.get("code") == "0" and str(data.get("sCode", "0")) == "0" and data.get("ordId"):
            self._recent[data["ordId"]] = inst_id
            if len(self._recent) > 50:
                self._recent.pop(next(iter(self._recent)))
            return {"id": data["ordId"], "client_order_id": cl, "symbol": body.get("symbol"),
                    "side": side, "type": "market", "state": "open"}

        msg = data.get("sMsg") or r.get("msg") or "order rejected"
        code = data.get("sCode") or r.get("code")
        return {"errors": [{"detail": str(msg), "code": str(code)}]}


# ----------------------------------------------------------------------------
# helpers for other scripts
# ----------------------------------------------------------------------------
_PUBLIC_CLIENT: Optional[OkxClient] = None


def current_ask(symbol: str) -> float:
    """Current best ask for a bot symbol like 'BTC-USD' (public endpoint, no keys needed)."""
    global _PUBLIC_CLIENT
    if _PUBLIC_CLIENT is None:
        _PUBLIC_CLIENT = OkxClient(creds=None)
    quote = _PUBLIC_CLIENT.settings["quote"]
    s = str(symbol).upper().strip()
    inst_id = s[:-4] + "-" + quote if s.endswith("-USD") else s
    t = _PUBLIC_CLIENT.ticker(inst_id)
    if not t or _f(t.get("askPx")) <= 0.0:
        raise RuntimeError(f"OKX ticker unavailable for {inst_id}")
    return float(t["askPx"])
