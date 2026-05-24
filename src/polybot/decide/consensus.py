"""Claude consensus vote on a candidate signal.

The monitor proves *wallets* agree. This layer asks Claude for a final sanity
check before risking money: is the market liquid, unresolved, and is the
consensus genuine rather than wash/coordinated noise? Returns a structured vote
the executor can gate on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from ..config import CONFIG
from ..monitor.monitor import Signal

MODEL = "claude-opus-4-7"

_SYSTEM = (
    "You are a risk gate for a Polymarket copy-trading bot. Multiple proven "
    "wallets just entered the same position. Decide whether the bot should ACT. "
    "Be skeptical: reject illiquid markets, near-resolved markets (price already "
    "near 0 or 1), and signals that look like wash trading or coordination. "
    "Respond ONLY with JSON: "
    '{"vote": "ENTER"|"SKIP", "confidence": 0.0-1.0, "reason": "<one sentence>"}'
)


@dataclass(frozen=True)
class Vote:
    enter: bool
    confidence: float
    reason: str


def vote_on_signal(signal: Signal, market: dict, *, client: Anthropic | None = None) -> Vote:
    client = client or Anthropic(api_key=CONFIG.anthropic_api_key)
    payload = {
        "title": signal.title,
        "outcome": signal.outcome,
        "consensus_price": signal.avg_price,
        "wallets_agreeing": len(signal.wallets),
        "market_volume": market.get("volume"),
        "market_liquidity": market.get("liquidity"),
        "outcome_prices": market.get("outcomePrices"),
        "closed": market.get("closed"),
    }
    msg = client.messages.create(
        model=MODEL,
        max_tokens=256,
        system=_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload)}],
    )
    text = msg.content[0].text.strip()
    try:
        data = json.loads(text)
        return Vote(
            enter=str(data.get("vote", "SKIP")).upper() == "ENTER",
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
        )
    except (json.JSONDecodeError, ValueError, KeyError):
        return Vote(enter=False, confidence=0.0, reason=f"unparseable vote: {text[:120]}")
