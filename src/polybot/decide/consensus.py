"""Claude consensus vote on a candidate signal.

The monitor proves *wallets* agree; this layer asks Claude for a final, web-grounded
sanity check before risking money: has the underlying event already been decided?
Is there breaking news? Is the market liquid and the consensus genuine?

Two backends (``CLAUDE_VOTE_BACKEND``):
- ``cli`` (default): shells out to the **Claude Code CLI** (`claude -p`), which uses
  your Claude **subscription** — no Platform API credits. WebSearch is enabled so
  the vote is still web-grounded.
- ``api``: calls the Anthropic SDK directly (pay-per-token Platform API).

A failed/unavailable vote returns SKIP — we never trade on a broken gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

from ..config import CONFIG
from ..monitor.monitor import Signal

MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
BACKEND = os.getenv("CLAUDE_VOTE_BACKEND", "cli").strip().lower()  # "cli" | "api"
CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "180"))

_SYSTEM = (
    "You are a risk gate for a Polymarket copy-trading bot whose sole purpose is to "
    "make money. Multiple proven wallets just entered the same position. Decide "
    "whether the bot should ACT.\n\n"
    "Use web search to check the CURRENT real-world state of this market before "
    "voting: has the underlying event already happened or been decided? Is there "
    "breaking news that makes the consensus stale or wrong? Is the implied "
    "probability out of line with reporting?\n\n"
    "Be skeptical: reject illiquid markets, markets already near-resolved (price "
    "near 0 or 1), events that have effectively concluded, and signals that look "
    "like wash trading or coordination. Favor ENTER only when the edge is real and "
    "current.\n\n"
    'After researching, respond with ONLY a JSON object, no other text: '
    '{"vote": "ENTER"|"SKIP", "confidence": 0.0-1.0, "reason": "<one sentence, '
    'citing what your search found>"}'
)


@dataclass(frozen=True)
class Vote:
    enter: bool
    confidence: float
    reason: str


def _payload(signal: Signal, market: dict) -> dict:
    return {
        "title": signal.title,
        "outcome": signal.outcome,
        "consensus_price": signal.avg_price,
        "wallets_agreeing": len(signal.wallets),
        "market_volume": market.get("volume"),
        "market_liquidity": market.get("liquidity"),
        "outcome_prices": market.get("outcomePrices"),
        "closed": market.get("closed"),
    }


def _parse_vote(text: str) -> Vote:
    """Parse the JSON verdict, tolerating prose around it."""
    candidate = text
    if not text.lstrip().startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
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


def _vote_cli(signal: Signal, market: dict) -> Vote:
    """Vote via the Claude Code CLI (uses the Claude subscription, not API credits)."""
    prompt = f"{_SYSTEM}\n\nSIGNAL:\n{json.dumps(_payload(signal, market))}"
    # Strip ANTHROPIC_API_KEY so the CLI uses the subscription, not (dead) API credits.
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        r = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--model", MODEL, "--allowedTools", "WebSearch,WebFetch"],
            capture_output=True, text=True, timeout=CLI_TIMEOUT, env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return Vote(False, 0.0, f"cli vote unavailable: {type(e).__name__}")
    if r.returncode != 0:
        return Vote(False, 0.0, f"cli vote error: {(r.stderr or r.stdout or '')[:120]}")
    text = r.stdout
    try:
        outer = json.loads(r.stdout)
        if isinstance(outer, dict) and "result" in outer:
            text = outer["result"]
    except json.JSONDecodeError:
        pass
    return _parse_vote(text)


def _vote_api(signal: Signal, market: dict) -> Vote:
    """Vote via the Anthropic Platform API (pay-per-token)."""
    from anthropic import Anthropic

    web_search = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}
    client = Anthropic(api_key=CONFIG.anthropic_api_key)
    messages = [{"role": "user", "content": json.dumps(_payload(signal, market))}]
    response = None
    for _ in range(4):
        response = client.messages.create(
            model=MODEL, max_tokens=2048,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[web_search], thinking={"type": "adaptive"},
            output_config={"effort": "medium"}, messages=messages,
        )
        if response.stop_reason != "pause_turn":
            break
        messages.append({"role": "assistant", "content": response.content})
    text = "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
    return _parse_vote(text)


def vote_on_signal(signal: Signal, market: dict, *, client=None) -> Vote:
    """Web-grounded ENTER/SKIP vote. Backend per ``CLAUDE_VOTE_BACKEND``."""
    if BACKEND == "api":
        return _vote_api(signal, market)
    return _vote_cli(signal, market)
