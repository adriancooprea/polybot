"""Real-time monitor of top wallets — fires a signal on consensus.

Polls each top wallet's recent activity, tracks every new BUY entry, and emits a
signal when ``MIN_WALLETS_AGREE`` distinct top wallets enter the *same* market on
the *same* side inside the ``AGREEMENT_WINDOW_MINUTES`` window.

You are not copying one wallet — you wait for consensus among proven wallets.
That filter alone kills most bad entries.
"""

from __future__ import annotations

import csv
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from ..config import CONFIG
from ..polymarket.client import Activity, PolymarketClient


@dataclass(frozen=True)
class Signal:
    market_id: str
    title: str
    outcome: str
    wallets: tuple[str, ...]
    avg_price: float
    triggered_at: int


@dataclass
class _Window:
    """Per-(market, outcome) sliding window of recent wallet entries.

    Entries arrive out of timestamp order (wallets polled sequentially, API
    paginates newest-first), so prune by *event time* against the newest
    timestamp seen — not by insertion (FIFO) order, which would drop the wrong
    entries and admit out-of-span ones.
    """

    entries: list[tuple[int, str, float]] = field(default_factory=list)  # (ts, wallet, price)

    def add(self, ts: int, wallet: str, price: float, horizon_s: int) -> None:
        self.entries.append((ts, wallet, price))
        newest = max(e[0] for e in self.entries)
        cutoff = newest - horizon_s
        self.entries = [e for e in self.entries if e[0] >= cutoff]

    def distinct_wallets(self) -> set[str]:
        return {w for _, w, _ in self.entries}

    def avg_price(self) -> float:
        prices = [p for _, _, p in self.entries if p]
        return sum(prices) / len(prices) if prices else 0.0


def load_top_wallets(csv_path: Path = CONFIG.top_wallets_csv) -> list[str]:
    """Read wallet addresses from the ranker's output CSV."""
    with csv_path.open() as f:
        return [row["wallet"].lower() for row in csv.DictReader(f)]


class Monitor:
    def __init__(
        self,
        wallets: list[str],
        client: PolymarketClient,
        *,
        min_agree: int = CONFIG.min_wallets_agree,
        window_minutes: int = CONFIG.agreement_window_minutes,
    ) -> None:
        self.wallets = [w.lower() for w in wallets]
        self.client = client
        self.min_agree = min_agree
        self.horizon_s = window_minutes * 60
        # Ignore activity older than start (minus one window) so booting the bot
        # doesn't fire signals on hours-old historical trades.
        self.since_ts = int(time.time()) - self.horizon_s
        self._seen_tx: set[str] = set()
        self._windows: dict[tuple[str, str], _Window] = defaultdict(_Window)
        self._fired: set[tuple[str, str, frozenset]] = set()

    def _ingest(self, act: Activity) -> Signal | None:
        if act.tx_hash in self._seen_tx or act.side != "BUY" or act.timestamp < self.since_ts:
            self._seen_tx.add(act.tx_hash)
            return None
        self._seen_tx.add(act.tx_hash)

        key = (act.market_id, act.outcome)
        win = self._windows[key]
        win.add(act.timestamp, act.wallet, act.price, self.horizon_s)

        wallets = win.distinct_wallets()
        if len(wallets) < self.min_agree:
            return None

        dedupe = (act.market_id, act.outcome, frozenset(wallets))
        if dedupe in self._fired:
            return None
        self._fired.add(dedupe)

        return Signal(
            market_id=act.market_id,
            title=act.title,
            outcome=act.outcome,
            wallets=tuple(sorted(wallets)),
            avg_price=round(win.avg_price(), 4),
            triggered_at=act.timestamp,
        )

    def poll_once(self) -> list[Signal]:
        """Poll every wallet once; return any signals that fired this round."""
        signals: list[Signal] = []
        for wallet in self.wallets:
            try:
                for act in self.client.activity(wallet):
                    sig = self._ingest(act)
                    if sig:
                        signals.append(sig)
            except Exception as exc:  # network hiccup on one wallet must not kill the loop
                print(f"  ! activity fetch failed for {wallet[:10]}…: {exc}")
        return signals

    def run(self, poll_seconds: int = 60):
        """Poll forever, yielding signals as they fire."""
        print(f"Monitoring {len(self.wallets)} wallets "
              f"(consensus={self.min_agree}, window={self.horizon_s // 60}m)")
        while True:
            for sig in self.poll_once():
                yield sig
            time.sleep(poll_seconds)


def main() -> None:
    wallets = load_top_wallets()
    with PolymarketClient() as client:
        mon = Monitor(wallets, client)
        for sig in mon.run():
            print(
                f"\n🔔 SIGNAL  {sig.title}\n"
                f"   outcome={sig.outcome}  avg_price={sig.avg_price}\n"
                f"   wallets={', '.join(w[:10] + '…' for w in sig.wallets)}"
            )


if __name__ == "__main__":
    main()
