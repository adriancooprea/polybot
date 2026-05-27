"""Central config, loaded from environment (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    polymarket_api_key: str = os.getenv("POLYMARKET_API_KEY", "")
    polymarket_api_secret: str = os.getenv("POLYMARKET_API_SECRET", "")
    polymarket_passphrase: str = os.getenv("POLYMARKET_PASSPHRASE", "")
    wallet_private_key: str = os.getenv("POLYMARKET_WALLET_PRIVATE_KEY", "")
    funder: str = os.getenv("POLYMARKET_FUNDER", "")
    signature_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "1"))
    chain_id: int = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))

    max_trades_per_day: int = int(os.getenv("MAX_TRADES_PER_DAY", "10"))
    agreement_window_minutes: int = int(os.getenv("AGREEMENT_WINDOW_MINUTES", "30"))
    min_wallets_agree: int = int(os.getenv("MIN_WALLETS_AGREE", "2"))
    # ignore wallet trades older than this — kills stale/resolved-market signals
    max_signal_age_minutes: int = int(os.getenv("MAX_SIGNAL_AGE_MINUTES", "60"))

    take_profit_pct: float = float(os.getenv("TAKE_PROFIT_PCT", "0.15"))
    stop_loss_pct: float = float(os.getenv("STOP_LOSS_PCT", "0.30"))
    # absolute price ceiling to bank a winner before resolution — percentage
    # take-profit is unreachable on high-priced favorites (e.g. 0.90 entry can't
    # gain 25% without exceeding the 1.0 cap). Set >=1.0 to disable.
    take_profit_price: float = float(os.getenv("TAKE_PROFIT_PRICE", "0.95"))
    # don't enter if the executable price has run more than this past consensus
    max_chase_pct: float = float(os.getenv("MAX_CHASE_PCT", "0.10"))
    # favorites-only floor: walk-forward backtest on the 2024-25 snapshot (3 OOS
    # splits) showed consensus has a robust edge on favorites (entry >= ~0.6,
    # ~91% win, +9.5% hold-to-resolution) and *negative* edge on longshots
    # (entry < 0.5). Skip entries below this price. Set 0.0 to disable.
    min_entry_price: float = float(os.getenv("MIN_ENTRY_PRICE", "0.0"))
    stake_usd: float = float(os.getenv("STAKE_USD", "20"))
    start_bankroll: float = float(os.getenv("START_BANKROLL", "200"))
    poll_seconds: int = int(os.getenv("POLL_SECONDS", "60"))
    dry_run: bool = _bool("DRY_RUN", True)

    top_wallets_csv: Path = DATA_DIR / "top_wallets.csv"
    open_trades_json: Path = DATA_DIR / "open_trades.json"


CONFIG = Config()
