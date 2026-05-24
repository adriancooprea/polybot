"""Fetch ground-truth market resolutions from the Gamma API.

Produces ``data/resolutions.csv`` (token_id,settlement) where settlement is 1.0
for the winning outcome token and 0.0 for the loser. Feed this to the ranker
(``rank --resolutions``) to replace the final-price heuristic with truth.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_DIR = Path(__file__).resolve().parents[3] / "data"


def _parse_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            return [str(v) for v in json.loads(value)]
        except json.JSONDecodeError:
            return []
    return []


def fetch_resolutions(out_csv: Path, *, page: int = 500, max_markets: int | None = None) -> int:
    """Page through closed markets and write token->settlement rows."""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    offset = 0
    with httpx.Client(timeout=30.0, follow_redirects=True) as http, out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["token_id", "settlement"])
        while True:
            r = http.get(f"{GAMMA_API}/markets",
                         params={"closed": "true", "limit": page, "offset": offset})
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            for m in batch:
                tokens = _parse_list(m.get("clobTokenIds"))
                prices = _parse_list(m.get("outcomePrices"))
                if len(tokens) != len(prices) or not tokens:
                    continue
                for tid, px in zip(tokens, prices):
                    try:
                        settle = 1.0 if float(px) >= 0.5 else 0.0
                    except ValueError:
                        continue
                    w.writerow([tid, settle])
                    written += 1
            offset += page
            print(f"\r  {offset} markets scanned, {written} tokens written", end="")
            if max_markets and offset >= max_markets:
                break
    print(f"\nWrote {written} resolutions to {out_csv}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Polymarket market resolutions.")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "resolutions.csv")
    ap.add_argument("--max-markets", type=int, default=None)
    args = ap.parse_args()
    fetch_resolutions(args.out, max_markets=args.max_markets)


if __name__ == "__main__":
    main()
