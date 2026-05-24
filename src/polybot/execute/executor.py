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

    def _check_risk(self, order: Order) -> None:
        if KILL_FILE.exists():
            raise RiskError(f"kill-switch active ({KILL_FILE})")
        if order.side == "BUY":  # only new entries are capped; exits always allowed
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
