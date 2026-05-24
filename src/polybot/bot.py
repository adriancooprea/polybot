"""polybot orchestrator — the live trading loop.

Wires the pipeline together:

    monitor (consensus)  ->  decide (Claude vote)  ->  execute (risk-gated)
                                                          |
    open positions  ->  exit_rules (take-profit / whale exodus)  -> close

Defaults to dry-run. Set DRY_RUN=false in .env to arm live trading.
"""

from __future__ import annotations

import time

from anthropic import Anthropic

from .config import CONFIG
from .decide.consensus import vote_on_signal
from .execute.executor import Executor, Order, RiskError
from .execute.exit_rules import evaluate_exit
from .monitor.monitor import Monitor, Signal, load_top_wallets
from .polymarket.client import PolymarketClient

DEFAULT_STAKE_USD = 20.0


class OpenTrade:
    def __init__(self, signal: Signal, entry_price: float, baseline: dict[str, float]) -> None:
        self.signal = signal
        self.entry_price = entry_price
        self.baseline = baseline


def _print_signal(sig: Signal) -> None:
    print(f"\n🔔 SIGNAL {sig.title} | {sig.outcome} @ {sig.avg_price} "
          f"| {len(sig.wallets)} wallets")


def run() -> None:
    wallets = load_top_wallets()
    anthropic = Anthropic(api_key=CONFIG.anthropic_api_key) if CONFIG.anthropic_api_key else None
    open_trades: dict[str, OpenTrade] = {}

    with PolymarketClient() as pm:
        monitor = Monitor(wallets, pm)
        executor = Executor()
        print(f"polybot live | dry_run={CONFIG.dry_run} | cap={CONFIG.max_trades_per_day}/day")

        while True:
            # 1) entries
            for sig in monitor.poll_once():
                _print_signal(sig)
                market = pm.market(sig.market_id)
                if anthropic is not None:
                    vote = vote_on_signal(sig, market, client=anthropic)
                    print(f"   Claude: {'ENTER' if vote.enter else 'SKIP'} "
                          f"({vote.confidence:.0%}) — {vote.reason}")
                    if not vote.enter:
                        continue
                try:
                    executor.place(Order(
                        market_id=sig.market_id, outcome=sig.outcome, side="BUY",
                        size_usd=DEFAULT_STAKE_USD, price=sig.avg_price,
                        reason=f"consensus:{len(sig.wallets)}",
                    ))
                    baseline = {w: 0.0 for w in sig.wallets}  # TODO: snapshot real sizes
                    open_trades[sig.market_id] = OpenTrade(sig, sig.avg_price, baseline)
                except RiskError as e:
                    print(f"   blocked: {e}")

            # 2) exits
            for market_id, trade in list(open_trades.items()):
                market = pm.market(market_id)
                prices = market.get("outcomePrices") or []
                current = float(prices[0]) if prices else trade.entry_price
                decision = evaluate_exit(
                    pm, market_id=market_id, entry_price=trade.entry_price,
                    current_price=current, trigger_wallets=trade.signal.wallets,
                    baseline_size=trade.baseline,
                )
                if decision.should_exit:
                    try:
                        executor.place(Order(
                            market_id=market_id, outcome=trade.signal.outcome, side="SELL",
                            size_usd=DEFAULT_STAKE_USD, price=current,
                            reason=decision.reason,
                        ))
                        print(f"   exit {market_id[:10]}… ({decision.reason})")
                        del open_trades[market_id]
                    except RiskError as e:
                        print(f"   exit blocked: {e}")

            time.sleep(60)


if __name__ == "__main__":
    run()
