"""Exit-rule unit tests (offline, no network)."""

from polybot.execute.exit_rules import (
    evaluate_exit,
    take_profit_hit,
    take_profit_price_hit,
    stop_loss_hit,
)


def test_take_profit_pct():
    assert take_profit_hit(0.50, 0.60, 0.15)       # +20% >= 15%
    assert not take_profit_hit(0.50, 0.55, 0.15)   # +10% < 15%
    assert not take_profit_hit(0.0, 0.60, 0.15)    # guard: zero entry


def test_stop_loss_pct():
    assert stop_loss_hit(0.50, 0.40, 0.15)         # -20% <= -15%
    assert not stop_loss_hit(0.50, 0.46, 0.15)     # -8% > -15%


def test_take_profit_price_ceiling():
    assert take_profit_price_hit(0.96, 0.95)       # at/above ceiling
    assert not take_profit_price_hit(0.94, 0.95)   # below ceiling
    # disabled when ceiling >= 1.0 (price can never exceed 1.0)
    assert not take_profit_price_hit(0.999, 1.0)


def test_high_priced_favorite_exits_on_abs_price_not_pct():
    """A 0.88 entry can't gain 25% (would need >1.0), so the percentage TP never
    fires — the absolute price ceiling is what banks it."""
    # No trigger wallets -> whale-exodus can't fire; isolate the price rules.
    d = evaluate_exit(
        client=None, market_id="m", entry_price=0.88, current_price=0.96,
        trigger_wallets=(), baseline_size={},
        take_profit_pct=0.25, stop_loss_pct=0.15, take_profit_price=0.95,
    )
    assert d.should_exit and d.reason == "take_profit_price"


def test_hold_when_nothing_triggers():
    d = evaluate_exit(
        client=None, market_id="m", entry_price=0.50, current_price=0.52,
        trigger_wallets=(), baseline_size={},
        take_profit_pct=0.25, stop_loss_pct=0.15, take_profit_price=0.95,
    )
    assert not d.should_exit and d.reason == "hold"
