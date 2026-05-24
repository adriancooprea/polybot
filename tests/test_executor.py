"""Risk-gate and state tests for the executor — no network, no real orders."""

from __future__ import annotations

import importlib

import pytest


def _fresh_executor(tmp_path, monkeypatch, **kw):
    """Re-import executor with DATA_DIR pointed at a temp dir."""
    monkeypatch.setenv("DRY_RUN", "true")
    import polybot.config as config
    importlib.reload(config)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    import polybot.execute.executor as ex
    importlib.reload(ex)
    monkeypatch.setattr(ex, "DATA_DIR", tmp_path)
    monkeypatch.setattr(ex, "KILL_FILE", tmp_path / "KILL")
    monkeypatch.setattr(ex, "TRADE_LOG", tmp_path / "trades.log.jsonl")
    return ex


def test_dry_run_buy_logs_not_sends(tmp_path, monkeypatch):
    ex = _fresh_executor(tmp_path, monkeypatch)
    e = ex.Executor(dry_run=True, max_per_day=10)
    status = e.place(ex.Order(market_id="M", outcome="Yes", side="BUY",
                              size_usd=20, price=0.4, reason="t", token_id="T", shares=50))
    assert status == "DRY_RUN"
    assert (tmp_path / "trades.log.jsonl").exists()


def test_daily_cap_blocks_buys(tmp_path, monkeypatch):
    ex = _fresh_executor(tmp_path, monkeypatch)
    e = ex.Executor(dry_run=True, max_per_day=2)
    for _ in range(2):
        e.place(ex.Order("M", "Yes", "BUY", 20, 0.4, "t", "T", 50))
    with pytest.raises(ex.RiskError, match="daily cap"):
        e.place(ex.Order("M", "Yes", "BUY", 20, 0.4, "t", "T", 50))


def test_exits_bypass_daily_cap(tmp_path, monkeypatch):
    ex = _fresh_executor(tmp_path, monkeypatch)
    e = ex.Executor(dry_run=True, max_per_day=1)
    e.place(ex.Order("M", "Yes", "BUY", 20, 0.4, "t", "T", 50))  # uses the 1 slot
    # SELL must still go through even though cap is reached
    assert e.place(ex.Order("M", "Yes", "SELL", 20, 0.5, "exit", "T", 50)) == "DRY_RUN"


def test_kill_switch_halts_all(tmp_path, monkeypatch):
    ex = _fresh_executor(tmp_path, monkeypatch)
    (tmp_path / "KILL").write_text("")
    e = ex.Executor(dry_run=True, max_per_day=10)
    with pytest.raises(ex.RiskError, match="kill-switch"):
        e.place(ex.Order("M", "Yes", "BUY", 20, 0.4, "t", "T", 50))


def test_trade_store_roundtrip(tmp_path, monkeypatch):
    import polybot.config as config
    importlib.reload(config)
    import polybot.state as state
    importlib.reload(state)
    path = tmp_path / "open.json"
    store = state.TradeStore(path=path)
    store.add(state.OpenTrade(market_id="M", token_id="T", outcome="Yes", title="x",
                              entry_price=0.4, shares=50, stake_usd=20,
                              trigger_wallets=["w1"], baseline_size={"w1": 5.0}))
    assert state.TradeStore(path=path).has("M")  # persisted across instances
    store.remove("M")
    assert not state.TradeStore(path=path).has("M")
