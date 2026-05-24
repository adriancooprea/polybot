"""Regression tests for code-review fixes."""

from __future__ import annotations

import csv
from pathlib import Path

from polybot.monitor.monitor import _Window
from polybot.rank.rank_wallets import rank_wallets


def test_window_prunes_by_event_time_not_insertion_order():
    w = _Window()
    w.add(1000, "a", 0.1, 1800)
    w.add(100, "b", 0.2, 1800)   # older, inserted after — within span of newest
    assert w.distinct_wallets() == {"a", "b"}
    w.add(5000, "c", 0.3, 1800)  # newest → cutoff 3200 drops a and b
    assert w.distinct_wallets() == {"c"}


def test_rank_cli_path_returns_ranking_not_copy_count(tmp_path: Path):
    trades = tmp_path / "of.csv"
    with trades.open("w", newline="") as f:
        csv.writer(f).writerows([
            ["timestamp", "maker", "makerAssetId", "makerAmountFilled",
             "taker", "takerAssetId", "takerAmountFilled", "transactionHash"],
            [1000, "0xBUY", "0", "30000000", "0xSELL", "T1", "100000000", "a"],
            [2000, "0xSELL2", "T1", "100000000", "0xBUY", "0", "98000000", "b"],
        ])
    rel = rank_wallets(trades, min_trades=1, min_win_rate=0.0, window_days=999999,
                       top_n=5, out_csv=tmp_path / "top.csv")
    cols = [d[0] for d in rel.description]
    assert "wallet" in cols and "Count" not in cols  # not the stale COPY result
    assert (tmp_path / "top.csv").exists()


def test_client_market_returns_dict_on_miss():
    from polybot.polymarket.client import PolymarketClient
    c = PolymarketClient.__new__(PolymarketClient)
    # empty outcomes/tokens must not raise (market() returns {} on miss)
    assert c.token_id_for({}, "Yes") is None
    assert c.outcome_price({}, "Yes") is None
