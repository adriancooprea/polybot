"""Thin Polymarket API client.

Three public surfaces are used:

- Data API   (https://data-api.polymarket.com)  — per-wallet activity & positions.
- Gamma API  (https://gamma-api.polymarket.com)  — market metadata & current odds.
- CLOB API   (https://clob.polymarket.com)        — order book / trade execution.

Only read endpoints are implemented here. Order placement lives in
``execute/`` and is gated behind risk checks + dry-run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass(frozen=True)
class Activity:
    """A single on-chain activity event for a wallet."""

    wallet: str
    timestamp: int
    market_id: str
    title: str
    side: str  # BUY / SELL
    outcome: str  # e.g. "Yes" / "No"
    size: float
    price: float
    tx_hash: str


class PolymarketClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self._http = httpx.Client(timeout=timeout, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PolymarketClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def activity(self, wallet: str, *, limit: int = 50) -> list[Activity]:
        """Recent activity for a wallet, newest first."""
        r = self._http.get(
            f"{DATA_API}/activity",
            params={"user": wallet.lower(), "limit": limit, "sortDirection": "DESC"},
        )
        r.raise_for_status()
        out: list[Activity] = []
        for e in r.json():
            if e.get("type") != "TRADE":
                continue
            out.append(
                Activity(
                    wallet=wallet.lower(),
                    timestamp=int(e.get("timestamp", 0)),
                    market_id=str(e.get("conditionId", "")),
                    title=str(e.get("title", "")),
                    side=str(e.get("side", "")).upper(),
                    outcome=str(e.get("outcome", "")),
                    size=float(e.get("size", 0) or 0),
                    price=float(e.get("price", 0) or 0),
                    tx_hash=str(e.get("transactionHash", "")),
                )
            )
        return out

    def positions(self, wallet: str) -> list[dict]:
        """Open positions for a wallet."""
        r = self._http.get(f"{DATA_API}/positions", params={"user": wallet.lower()})
        r.raise_for_status()
        return r.json()

    def market(self, condition_id: str) -> dict:
        """Market metadata incl. current outcome prices."""
        r = self._http.get(f"{GAMMA_API}/markets", params={"condition_ids": condition_id})
        r.raise_for_status()
        data = r.json()
        return data[0] if isinstance(data, list) and data else data

    def midpoint(self, token_id: str) -> float | None:
        """Current midpoint price for an outcome token from the CLOB."""
        r = self._http.get(f"{CLOB_API}/midpoint", params={"token_id": token_id})
        if r.status_code != 200:
            return None
        return float(r.json().get("mid", 0) or 0)

    @staticmethod
    def _parse_list(value) -> list[str]:
        """Gamma returns ``outcomes``/``clobTokenIds`` as JSON-encoded strings."""
        if isinstance(value, list):
            return [str(v) for v in value]
        if isinstance(value, str) and value:
            try:
                return [str(v) for v in json.loads(value)]
            except json.JSONDecodeError:
                return []
        return []

    def token_id_for(self, market: dict, outcome: str) -> str | None:
        """Resolve the CLOB token id for a named outcome in a market."""
        outcomes = self._parse_list(market.get("outcomes"))
        token_ids = self._parse_list(market.get("clobTokenIds"))
        if len(outcomes) != len(token_ids):
            return None
        target = outcome.strip().lower()
        for name, tid in zip(outcomes, token_ids):
            if name.strip().lower() == target:
                return tid
        return None

    def outcome_price(self, market: dict, outcome: str) -> float | None:
        """Current price of a named outcome from Gamma's ``outcomePrices``."""
        outcomes = self._parse_list(market.get("outcomes"))
        prices = self._parse_list(market.get("outcomePrices"))
        if len(outcomes) != len(prices):
            return None
        target = outcome.strip().lower()
        for name, px in zip(outcomes, prices):
            if name.strip().lower() == target:
                return float(px)
        return None
