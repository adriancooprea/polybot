"""Persistent store of open trades, so a restart resumes mid-flight positions."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .config import CONFIG


@dataclass
class OpenTrade:
    market_id: str
    token_id: str
    outcome: str
    title: str
    entry_price: float
    shares: float
    stake_usd: float
    trigger_wallets: list[str] = field(default_factory=list)
    baseline_size: dict[str, float] = field(default_factory=dict)


class TradeStore:
    def __init__(self, path=CONFIG.open_trades_json) -> None:
        self.path = path
        self._trades: dict[str, OpenTrade] = self._load()

    def _load(self) -> dict[str, OpenTrade]:
        if not self.path.exists():
            return {}
        with self.path.open() as f:
            raw = json.load(f)
        return {k: OpenTrade(**v) for k, v in raw.items()}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w") as f:
            json.dump({k: asdict(v) for k, v in self._trades.items()}, f, indent=2)

    def all(self) -> dict[str, OpenTrade]:
        return dict(self._trades)

    def has(self, market_id: str) -> bool:
        return market_id in self._trades

    def add(self, trade: OpenTrade) -> None:
        self._trades[trade.market_id] = trade
        self._save()

    def remove(self, market_id: str) -> None:
        self._trades.pop(market_id, None)
        self._save()
