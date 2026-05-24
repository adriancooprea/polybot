"""Exit logic — close before the whales do.

A position closes on whichever fires first:

1. **Take-profit**: unrealized PnL >= ``TAKE_PROFIT_PCT`` (default 15%).
2. **Stop-loss**: unrealized PnL <= ``-STOP_LOSS_PCT`` (default 30%) — cap the
   downside so a losing position can't ride to zero.
3. **Whale exodus**: the top wallets that triggered the entry start *reducing*
   their position size in this market.

Exiting before the crowd reverses is what separates this from naive copy-trading.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CONFIG
from ..polymarket.client import PolymarketClient


@dataclass(frozen=True)
class ExitDecision:
    should_exit: bool
    reason: str  # "take_profit" | "whale_exodus" | "hold"


def take_profit_hit(entry_price: float, current_price: float, threshold: float) -> bool:
    if entry_price <= 0:
        return False
    return (current_price - entry_price) / entry_price >= threshold


def stop_loss_hit(entry_price: float, current_price: float, threshold: float) -> bool:
    if entry_price <= 0:
        return False
    return (current_price - entry_price) / entry_price <= -threshold


def whales_reducing(
    client: PolymarketClient,
    market_id: str,
    trigger_wallets: tuple[str, ...],
    *,
    baseline_size: dict[str, float],
    min_reducers: int = 1,
) -> bool:
    """True if at least ``min_reducers`` trigger wallets have shrunk their
    position in this market below the size recorded at entry.
    """
    reducers = 0
    for wallet in trigger_wallets:
        try:
            positions = client.positions(wallet)
        except Exception:
            continue
        size_now = sum(
            float(p.get("size", 0) or 0)
            for p in positions
            if str(p.get("conditionId", "")) == market_id
        )
        if size_now < baseline_size.get(wallet, 0.0) * 0.999:  # tolerance for fees/rounding
            reducers += 1
    return reducers >= min_reducers


def evaluate_exit(
    client: PolymarketClient,
    *,
    market_id: str,
    entry_price: float,
    current_price: float,
    trigger_wallets: tuple[str, ...],
    baseline_size: dict[str, float],
    take_profit_pct: float = CONFIG.take_profit_pct,
    stop_loss_pct: float = CONFIG.stop_loss_pct,
) -> ExitDecision:
    """Apply all rules; first match wins."""
    if take_profit_hit(entry_price, current_price, take_profit_pct):
        return ExitDecision(True, "take_profit")
    if stop_loss_hit(entry_price, current_price, stop_loss_pct):
        return ExitDecision(True, "stop_loss")
    if whales_reducing(client, market_id, trigger_wallets, baseline_size=baseline_size):
        return ExitDecision(True, "whale_exodus")
    return ExitDecision(False, "hold")
