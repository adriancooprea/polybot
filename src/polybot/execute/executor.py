"""Trade executor — the danger zone.

Every order passes risk checks before it can touch the CLOB:

- Daily trade cap (``MAX_TRADES_PER_DAY``).
- Global kill-switch (a ``KILL`` file in the data dir halts all trading).
- Dry-run default: orders are logged, not sent, unless ``DRY_RUN=false``.

No code path places a real order without clearing all three. When live, orders
route to ``TradeClient`` (py-clob-client) as market FOK orders.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from ..config import CONFIG, DATA_DIR

KILL_FILE = DATA_DIR / "KILL"
TRADE_LOG = DATA_DIR / "trades.log.jsonl"


@dataclass(frozen=True)
class Order:
    market_id: str
    outcome: str
    side: str  # BUY / SELL
    size_usd: float  # USD for BUY; for SELL this is informational
    price: float
    reason: str
    token_id: str = ""
    shares: float = 0.0  # required for SELL (how many shares to offload)


def _today() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d")


def _trades_today() -> int:
    if not TRADE_LOG.exists():
        return 0
    today = _today()
    n = 0
    with TRADE_LOG.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # only entries count against the daily cap
            if (rec.get("day") == today and rec.get("side") == "BUY"
                    and rec.get("status") in {"FILLED", "DRY_RUN"}):
                n += 1
    return n


def _open_buy_in_log(token_id: str) -> bool:
    """True if the trade log shows a filled BUY on ``token_id`` not yet offset by
    a filled SELL — i.e. the position is already open or opening.

    Guards against double-entry: a market order returns "delayed"/success but the
    shares can take a while to surface in positions(), so neither the in-memory
    store nor reconcile knows we hold it yet. A repeat signal in that gap would
    place a second order on the same outcome (observed: $2+$2 = $4 positions).
    The trade log is written the instant an order is accepted, so it closes the
    gap and survives restarts. Keyed on token_id, so opposite outcomes (Yes/No)
    of the same market are still allowed.
    """
    if not token_id or not TRADE_LOG.exists():
        return False
    last_buy = last_sell = ""
    with TRADE_LOG.open() as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("token_id") != token_id or rec.get("status") not in {"FILLED", "DRY_RUN"}:
                continue
            if rec.get("side") == "BUY":
                last_buy = rec.get("ts", "")
            elif rec.get("side") == "SELL":
                last_sell = rec.get("ts", "")
    return bool(last_buy) and last_buy > last_sell  # ISO ts compare; "" sorts first


class RiskError(RuntimeError):
    """Raised when an order is blocked by a risk check."""


class Executor:
    def __init__(self, *, dry_run: bool = CONFIG.dry_run,
                 max_per_day: int = CONFIG.max_trades_per_day, trade_client=None) -> None:
        self.dry_run = dry_run
        self.max_per_day = max_per_day
        self._trade_client = trade_client  # lazily built TradeClient when live
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _client(self):
        if self._trade_client is None:
            from ..polymarket.clob import TradeClient
            self._trade_client = TradeClient()
        return self._trade_client

    def last_fill_price(self, token_id: str, side: str) -> float | None:
        """Actual price of our most recent fill on ``token_id`` for ``side`` (live only).

        Reads the CLOB's authoritative trade record so the journal logs real fills
        (which can slip far from the midpoint on thin books), not the intended price.
        """
        if self.dry_run or self._trade_client is None:
            return None
        try:
            tr = self._trade_client._client.get_trades()
            rows = tr.get("data", tr) if isinstance(tr, dict) else tr
            for t in rows or []:
                if str(t.get("asset_id")) == token_id and str(t.get("side", "")).upper() == side.upper():
                    return float(t.get("price", 0) or 0)
        except Exception:
            pass
        return None

    def _check_risk(self, order: Order) -> None:
        if KILL_FILE.exists():
            raise RiskError(f"kill-switch active ({KILL_FILE})")
        if order.side == "BUY":  # only new entries are capped; exits always allowed
            if _open_buy_in_log(order.token_id):
                raise RiskError("duplicate entry: already hold/opening this position")
            n = _trades_today()
            if n >= self.max_per_day:
                raise RiskError(f"daily cap reached ({n}/{self.max_per_day})")

    def _log(self, order: Order, status: str, extra: dict | None = None) -> None:
        rec = {"day": _today(), "ts": dt.datetime.now(dt.UTC).isoformat(),
               "status": status, **asdict(order), **(extra or {})}
        with TRADE_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def place(self, order: Order) -> str:
        """Place an order. Returns the resulting status."""
        self._check_risk(order)

        if self.dry_run:
            self._log(order, "DRY_RUN")
            print(f"[DRY_RUN] {order.side} {order.outcome} ${order.size_usd:.2f} @ {order.price}")
            return "DRY_RUN"

        if not order.token_id:
            raise RiskError("live order missing token_id")
        if order.side == "SELL" and order.shares <= 0:
            raise RiskError("live SELL requires shares > 0")
        client = self._client()
        try:
            if order.side == "BUY":
                fill = client.market_buy(order.token_id, order.size_usd)
            else:
                fill = client.market_sell(order.token_id, order.shares)
        except RiskError:
            raise
        except Exception as e:  # SDK/CLOB error (e.g. FOK kill) must not crash the bot
            self._log(order, "ERROR", {"error": str(e)[:300]})
            print(f"[ERROR] {order.side} {order.outcome}: {str(e)[:160]}")
            raise RiskError(f"order failed: {str(e)[:160]}")

        status = "FILLED" if fill.ok else "REJECTED"
        self._log(order, status, {"order_id": fill.order_id, "clob_status": fill.status})
        print(f"[{status}] {order.side} {order.outcome} @ {order.price} ({fill.status})")
        if not fill.ok:
            raise RiskError(f"order rejected: {fill.status}")
        return status
