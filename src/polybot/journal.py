"""Append-only journals — the data layer for human-in-the-loop learning.

The bot records every decision and every closed trade; later we analyze these
together (``polybot report``) and evolve the strategy/code. Nothing here changes
trading behavior — it only records.

- ``data/decisions.jsonl`` — every Claude vote (ENTER *and* SKIP) with context,
  so we can see what the gate is approving/rejecting and why.
- ``data/journal.jsonl``   — every closed trade with realized PnL and attribution
  (which trigger wallets, entry/exit, exit reason), so we can score what works.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import defaultdict

from .config import DATA_DIR

DECISIONS = DATA_DIR / "decisions.jsonl"
JOURNAL = DATA_DIR / "journal.jsonl"


def _ts() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _append(path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def log_decision(signal, vote, market: dict) -> None:
    """Record a vote (ENTER or SKIP) with the context it was made on."""
    _append(DECISIONS, {
        "ts": _ts(),
        "market_id": signal.market_id,
        "title": signal.title,
        "outcome": signal.outcome,
        "consensus_price": signal.avg_price,
        "n_wallets": len(signal.wallets),
        "wallets": list(signal.wallets),
        "vote": "ENTER" if vote.enter else "SKIP",
        "confidence": vote.confidence,
        "reason": vote.reason,
        "market_volume": market.get("volume"),
        "market_liquidity": market.get("liquidity"),
    })


def log_close(trade, *, exit_price: float, exit_reason: str, shares_sold: float) -> float:
    """Record a closed trade with realized PnL. Returns the PnL."""
    pnl = round((exit_price - trade.entry_price) * shares_sold, 4)
    _append(JOURNAL, {
        "ts": _ts(),
        "market_id": trade.market_id,
        "title": trade.title,
        "outcome": trade.outcome,
        "entry_price": trade.entry_price,
        "exit_price": round(exit_price, 4),
        "shares": round(shares_sold, 4),
        "stake_usd": trade.stake_usd,
        "realized_pnl": pnl,
        "return_pct": round((exit_price / trade.entry_price - 1) * 100, 2) if trade.entry_price else None,
        "exit_reason": exit_reason,
        "trigger_wallets": list(trade.trigger_wallets),
    })
    return pnl


def _read(path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open() as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def summarize() -> str:
    """Human-readable report over the journals — our analysis starting point."""
    trades = _read(JOURNAL)
    decisions = _read(DECISIONS)
    lines: list[str] = ["polybot journal report", "=" * 40]

    if not trades:
        lines.append("No closed trades recorded yet.")
    else:
        pnl = sum(t["realized_pnl"] for t in trades)
        wins = [t for t in trades if t["realized_pnl"] > 0]
        lines += [
            f"Closed trades: {len(trades)}",
            f"Total realized PnL: ${pnl:+.2f}",
            f"Win rate: {len(wins)/len(trades)*100:.0f}%  ({len(wins)}W / {len(trades)-len(wins)}L)",
            f"Avg win: ${(sum(t['realized_pnl'] for t in wins)/len(wins)):+.2f}" if wins else "Avg win: —",
        ]
        # by exit reason
        by_reason: dict[str, list] = defaultdict(list)
        for t in trades:
            by_reason[t.get("exit_reason", "?")].append(t["realized_pnl"])
        lines.append("\nBy exit reason:")
        for r, pls in sorted(by_reason.items()):
            lines.append(f"  {r:12} n={len(pls):3}  PnL ${sum(pls):+.2f}")
        # by trigger wallet (copied-trade attribution — the key learning signal)
        by_wallet: dict[str, list] = defaultdict(list)
        for t in trades:
            for w in t.get("trigger_wallets", []):
                by_wallet[w].append(t["realized_pnl"])
        ranked = sorted(by_wallet.items(), key=lambda kv: sum(kv[1]))
        lines.append("\nWorst trigger wallets (copied-trade PnL):")
        for w, pls in ranked[:5]:
            lines.append(f"  {w[:12]}…  n={len(pls):2}  ${sum(pls):+.2f}")
        lines.append("Best trigger wallets:")
        for w, pls in ranked[-5:][::-1]:
            lines.append(f"  {w[:12]}…  n={len(pls):2}  ${sum(pls):+.2f}")

    if decisions:
        enters = sum(1 for d in decisions if d["vote"] == "ENTER")
        lines += ["", f"Decisions logged: {len(decisions)}  (ENTER {enters} / SKIP {len(decisions)-enters})"]
    return "\n".join(lines)


def main() -> None:
    print(summarize())


if __name__ == "__main__":
    main()
