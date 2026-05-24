"""Trade executor — the danger zone.

Every order passes risk checks before it can touch the CLOB:

- Daily trade cap (``MAX_TRADES_PER_DAY``).
- Global kill-switch (a ``KILL`` file in the data dir halts all trading).
- Dry-run default: orders are logged, not sent, unless ``DRY_RUN=false``.

No code path places a real order without clearing all three.
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
    size_usd: float
    price: float
    reason: str


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
            if rec.get("day") == today and rec.get("status") in {"FILLED", "DRY_RUN"}:
                n += 1
    return n


class RiskError(RuntimeError):
    """Raised when an order is blocked by a risk check."""


class Executor:
    def __init__(self, *, dry_run: bool = CONFIG.dry_run,
                 max_per_day: int = CONFIG.max_trades_per_day) -> None:
        self.dry_run = dry_run
        self.max_per_day = max_per_day
        DATA_DIR.mkdir(parents=True, exist_ok=True)

    def _check_risk(self) -> None:
        if KILL_FILE.exists():
            raise RiskError(f"kill-switch active ({KILL_FILE})")
        n = _trades_today()
        if n >= self.max_per_day:
            raise RiskError(f"daily cap reached ({n}/{self.max_per_day})")

    def _log(self, order: Order, status: str) -> None:
        rec = {"day": _today(), "ts": dt.datetime.now(dt.UTC).isoformat(),
               "status": status, **asdict(order)}
        with TRADE_LOG.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def place(self, order: Order) -> str:
        """Place an order. Returns the resulting status."""
        self._check_risk()
        if self.dry_run:
            self._log(order, "DRY_RUN")
            print(f"[DRY_RUN] {order.side} {order.outcome} ${order.size_usd} @ {order.price}")
            return "DRY_RUN"
        # TODO: real CLOB order via py-clob-client, signed with wallet key.
        raise NotImplementedError("Live order placement not implemented — keep DRY_RUN=true.")
