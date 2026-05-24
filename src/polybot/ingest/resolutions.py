"""Fetch ground-truth market resolutions from the Gamma API.

Produces ``data/resolutions.csv`` (token_id,settlement) where settlement is 1.0
for the winning outcome token and 0.0 for the loser. Feed this to the ranker
(``rank --resolutions``) to replace the final-price heuristic with truth.

Gamma's ``/markets`` endpoint caps ``offset`` at 10000 (offset>10000 -> HTTP 422)
and defaults to *oldest-first*, so naive offset paging both crashes and returns
only ancient markets. Instead we page within ``end_date`` windows and recursively
bisect any window dense enough to exceed the offset cap, so coverage is complete
and order-independent. Token ids are globally de-duped (a token belongs to one
market) so overlapping windows can never write a duplicate settlement row.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from pathlib import Path

import httpx

GAMMA_API = "https://gamma-api.polymarket.com"
DATA_DIR = Path(__file__).resolve().parents[3] / "data"

OFFSET_CAP = 10000  # Gamma rejects offset > 10000 with 422
DEFAULT_START = "2024-01-01"  # covers the snapshot's recent window + margin


def _parse_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str) and value:
        try:
            return [str(v) for v in json.loads(value)]
        except json.JSONDecodeError:
            return []
    return []


def _iso(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _overflows(http: httpx.Client, start_ts: int, end_ts: int) -> bool:
    """True if the window holds more markets than the offset cap can page."""
    r = http.get(f"{GAMMA_API}/markets", params={
        "closed": "true", "limit": 1, "offset": OFFSET_CAP,
        "end_date_min": _iso(start_ts), "end_date_max": _iso(end_ts)})
    r.raise_for_status()
    return bool(r.json())


def _harvest_window(http: httpx.Client, start_ts: int, end_ts: int, w, seen: set[str],
                    *, page: int) -> int:
    """Page one window fully by offset. Returns tokens written."""
    written = 0
    offset = 0
    while offset <= OFFSET_CAP:
        r = http.get(f"{GAMMA_API}/markets", params={
            "closed": "true", "limit": page, "offset": offset,
            "end_date_min": _iso(start_ts), "end_date_max": _iso(end_ts)})
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
                if tid in seen:
                    continue
                try:
                    settle = 1.0 if float(px) >= 0.5 else 0.0
                except ValueError:
                    continue
                seen.add(tid)
                w.writerow([tid, settle])
                written += 1
        offset += page
    return written


def fetch_resolutions(out_csv: Path, *, start: str = DEFAULT_START, end: str | None = None,
                      page: int = 500) -> int:
    """Write token->settlement rows for closed markets ending in [start, end).

    Walks ``end_date`` windows, bisecting any window too dense for the offset cap.
    """
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    start_ts = int(dt.datetime.fromisoformat(start).replace(tzinfo=dt.timezone.utc).timestamp())
    end_ts = (int(dt.datetime.fromisoformat(end).replace(tzinfo=dt.timezone.utc).timestamp())
              if end else int(dt.datetime.now(dt.timezone.utc).timestamp()) + 86400)

    seen: set[str] = set()
    written = 0
    with httpx.Client(timeout=30.0, follow_redirects=True) as http, out_csv.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["token_id", "settlement"])
        # DFS over a stack of date windows; split any that overflow the cap.
        stack: list[tuple[int, int]] = [(start_ts, end_ts)]
        while stack:
            a, b = stack.pop()
            if b - a > 1 and _overflows(http, a, b):
                mid = (a + b) // 2
                stack.append((mid, b))
                stack.append((a, mid))
                continue
            written += _harvest_window(http, a, b, w, seen, page=page)
            print(f"\r  window {_iso(a)[:10]}..{_iso(b)[:10]}: {written} tokens total", end="")
    print(f"\nWrote {written} resolutions to {out_csv}")
    return written


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Polymarket market resolutions.")
    ap.add_argument("--out", type=Path, default=DATA_DIR / "resolutions.csv")
    ap.add_argument("--start", default=DEFAULT_START, help="earliest market end date (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="latest market end date (YYYY-MM-DD); default now")
    args = ap.parse_args()
    fetch_resolutions(args.out, start=args.start, end=args.end)


if __name__ == "__main__":
    main()
