"""Claude consensus vote on a candidate signal.

The monitor proves *wallets* agree. This layer asks Claude for a final sanity
check before risking money: is the market liquid, unresolved, and is the
consensus genuine rather than wash/coordinated noise? Crucially, Claude is given
the **web_search** server tool so it can check current real-world news before
voting — prediction markets move on events, and the snapshot/consensus may be
stale relative to breaking news. Returns a structured vote the executor gates on.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from anthropic import Anthropic

from ..config import CONFIG
from ..monitor.monitor import Signal

# Sonnet 4.6 by default — strong judgment, ~5x cheaper than Opus, and ample for a
# web-grounded risk gate. Override with CLAUDE_MODEL (e.g. claude-opus-4-7).
MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")

# Server-side web search (latest version → dynamic filtering). Bounded so a single
# vote can't run up unbounded search cost.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
# Server tools run a server-side loop; it can pause at 10 iterations (pause_turn).
# Re-send to resume, capped so we never loop forever.
MAX_CONTINUATIONS = 3

# System prompt is frozen → cache it. (At ~150 tokens this is below Sonnet 4.6's
# 2048-token cache minimum, so it won't actually cache until the prompt grows;
# the breakpoint is correct placement and engages for free once it does.)
_SYSTEM = [
    {
        "type": "text",
        "text": (
            "You are a risk gate for a Polymarket copy-trading bot whose sole purpose "
            "is to make money. Multiple proven wallets just entered the same position. "
            "Decide whether the bot should ACT.\n\n"
            "Use the web_search tool to check the CURRENT real-world state of this "
            "market before voting: has the underlying event already happened or been "
            "decided? Is there breaking news that makes the consensus stale or wrong? "
            "Is the implied probability out of line with what reporting suggests?\n\n"
            "Be skeptical: reject illiquid markets, markets already near-resolved "
            "(price near 0 or 1), events that have effectively concluded, and signals "
            "that look like wash trading or coordination. Favor ENTER only when the "
            "edge is real and current.\n\n"
            'After researching, respond with ONLY a JSON object, no other text: '
            '{"vote": "ENTER"|"SKIP", "confidence": 0.0-1.0, "reason": "<one sentence, '
            'citing what your search found>"}'
        ),
        "cache_control": {"type": "ephemeral"},
    }
]


@dataclass(frozen=True)
class Vote:
    enter: bool
    confidence: float
    reason: str


def _final_text(response) -> str:
    """Concatenate the text blocks of a response (skipping tool-use/search/thinking)."""
    return "\n".join(
        b.text for b in response.content if getattr(b, "type", None) == "text"
    ).strip()


def _parse_vote(text: str) -> Vote:
    """Parse the JSON verdict, tolerating prose around it."""
    candidate = text
    if not text.lstrip().startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)  # pull the JSON object out of prose
        if m:
            candidate = m.group(0)
    try:
        data = json.loads(candidate)
        return Vote(
            enter=str(data.get("vote", "SKIP")).upper() == "ENTER",
            confidence=float(data.get("confidence", 0.0)),
            reason=str(data.get("reason", "")),
        )
    except (json.JSONDecodeError, ValueError, KeyError):
        return Vote(enter=False, confidence=0.0, reason=f"unparseable vote: {text[:120]}")


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
    messages = [{"role": "user", "content": json.dumps(payload)}]

    response = None
    for _ in range(MAX_CONTINUATIONS + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,  # room for search-tool turns; final JSON is tiny
            system=_SYSTEM,
            tools=[WEB_SEARCH_TOOL],
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        # server-tool loop paused — append and resume (server picks up automatically)
        messages.append({"role": "assistant", "content": response.content})

    return _parse_vote(_final_text(response))
