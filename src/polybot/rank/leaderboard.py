"""Fresh wallet ranking from Polymarket's live profit leaderboard.

The snapshot ranker (``rank/rank_wallets.py``) scores the full universe but on
data that ends 2025-10. This module instead pulls Polymarket's *current* profit
leaderboard — authoritative, server-computed realized profit per wallet — and
emits a ``top_wallets.csv`` the monitor can consume directly.

Why not reconstruct win-rate/profit-factor per wallet (the richer metrics the
snapshot ranker produces)? Because the free Data API can't support it: ``/positions``
is survivorship-biased (winners get redeemed and drop off), and ``/activity``
(TRADE events) does not reconcile with the leaderboard profit — it omits
redemptions/rewards, so a high-profit wallet's trade legs sum to a tiny or
negative number. The leaderboard ``amount`` is the only authoritative, current
profit figure available without a paid subgraph gateway. So we rank on that.

The leaderboard caps at 50 rows per window; we union several windows to widen
the proven-wallet set, ranking by all-time profit (30d as tiebreak).
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import httpx

LB_API = "https://lb-api.polymarket.com"
DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def _leaderboard(http: httpx.Client, metric: str, window: str) -> list[dict]:
    """One leaderboard page (caps at 50 rows). metric ∈ {profit, volume}."""
    r = http.get(f"{LB_API}/{metric}", params={"window": window, "limit": 50})
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def fetch_leaderboard(out_csv: Path = DATA_DIR / "top_wallets.csv",
                      *, windows: tuple[str, ...] = ("all", "30d", "7d")) -> int:
    """Union profit leaderboards across windows; write ranked wallets. Returns count."""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    wallets: dict[str, dict] = {}
    with httpx.Client(timeout=30.0, follow_redirects=True) as http:
        for w in windows:
            for row in _leaderboard(http, "profit", w):
                addr = str(row.get("proxyWallet", "")).lower()
                if not addr:
                    continue
                rec = wallets.setdefault(addr, {"wallet": addr,
                                                "pseudonym": row.get("pseudonym", "")})
                rec[f"profit_{w}"] = round(float(row.get("amount", 0) or 0), 2)

    cols = ["wallet", "pseudonym", *(f"profit_{w}" for w in windows)]
    ranked = sorted(wallets.values(),
                    key=lambda r: r.get("profit_all", r.get(f"profit_{windows[0]}", 0)),
                    reverse=True)
    with out_csv.open("w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for rec in ranked:
            wr.writerow({c: rec.get(c, "") for c in cols})
    print(f"Wrote {len(ranked)} wallets to {out_csv} (union of windows: {', '.join(windows)})")
    return len(ranked)


def main() -> None:
    ap = argparse.ArgumentParser(description="Fresh wallet ranking from the Polymarket leaderboard.")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "top_wallets.csv")
    ap.add_argument("--windows", nargs="+", default=["all", "30d", "7d"],
                    help="leaderboard windows to union (e.g. all 30d 7d 1d)")
    args = ap.parse_args()
    fetch_leaderboard(args.out, windows=tuple(args.windows))


if __name__ == "__main__":
    main()
