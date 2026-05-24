"""polybot orchestrator — the live trading loop.

Wires the pipeline together:

    monitor (consensus)  ->  decide (Claude vote)  ->  execute (risk-gated)
                                                          |
    open positions  ->  exit_rules (take-profit / whale exodus)  -> close

Open trades persist to disk, so a restart resumes positions mid-flight.
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
from .state import OpenTrade, TradeStore


def _whale_position_sizes(pm: PolymarketClient, market_id: str,
                          wallets: tuple[str, ...]) -> dict[str, float]:
    """Snapshot each trigger wallet's current size in this market (exit baseline)."""
    sizes: dict[str, float] = {}
    for w in wallets:
        try:
            positions = pm.positions(w)
        except Exception:
            sizes[w] = 0.0
            continue
        sizes[w] = sum(float(p.get("size", 0) or 0)
                       for p in positions if str(p.get("conditionId", "")) == market_id)
    return sizes


def _handle_signal(sig: Signal, pm: PolymarketClient, executor: Executor,
                   store: TradeStore, anthropic: Anthropic | None) -> None:
    if store.has(sig.market_id):
        return  # already holding this market

    print(f"\n🔔 SIGNAL {sig.title} | {sig.outcome} @ {sig.avg_price} "
          f"| {len(sig.wallets)} wallets")

    market = pm.market(sig.market_id)
    if not isinstance(market, dict) or not market:
        # Gamma returns [] for archived/unknown markets (e.g. long-resolved ones)
        print(f"   skip: no market metadata for {sig.market_id}")
        return
    if anthropic is not None:
        vote = vote_on_signal(sig, market, client=anthropic)
        print(f"   Claude: {'ENTER' if vote.enter else 'SKIP'} "
              f"({vote.confidence:.0%}) — {vote.reason}")
        if not vote.enter:
            return

    token_id = pm.token_id_for(market, sig.outcome)
    if not token_id:
        print(f"   skip: could not resolve token_id for outcome '{sig.outcome}'")
        return

    price = pm.outcome_price(market, sig.outcome) or sig.avg_price
    stake = CONFIG.stake_usd
    shares = stake / price if price > 0 else 0.0

    try:
        executor.place(Order(
            market_id=sig.market_id, outcome=sig.outcome, side="BUY",
            size_usd=stake, price=price, reason=f"consensus:{len(sig.wallets)}",
            token_id=token_id, shares=shares,
        ))
    except RiskError as e:
        print(f"   blocked: {e}")
        return

    store.add(OpenTrade(
        market_id=sig.market_id, token_id=token_id, outcome=sig.outcome,
        title=sig.title, entry_price=price, shares=shares, stake_usd=stake,
        trigger_wallets=list(sig.wallets),
        baseline_size=_whale_position_sizes(pm, sig.market_id, sig.wallets),
    ))


def _handle_exits(pm: PolymarketClient, executor: Executor, store: TradeStore) -> None:
    for market_id, trade in store.all().items():
        market = pm.market(market_id)
        current = pm.outcome_price(market, trade.outcome) or trade.entry_price
        decision = evaluate_exit(
            pm, market_id=market_id, entry_price=trade.entry_price,
            current_price=current, trigger_wallets=tuple(trade.trigger_wallets),
            baseline_size=trade.baseline_size,
        )
        if not decision.should_exit:
            continue
        try:
            executor.place(Order(
                market_id=market_id, outcome=trade.outcome, side="SELL",
                size_usd=trade.shares * current, price=current,
                reason=decision.reason, token_id=trade.token_id, shares=trade.shares,
            ))
            print(f"   exit {trade.title[:32]} ({decision.reason})")
            store.remove(market_id)
        except RiskError as e:
            print(f"   exit blocked: {e}")


def run() -> None:
    wallets = load_top_wallets()
    anthropic = Anthropic(api_key=CONFIG.anthropic_api_key) if CONFIG.anthropic_api_key else None
    store = TradeStore()

    with PolymarketClient() as pm:
        monitor = Monitor(wallets, pm)
        executor = Executor()
        print(f"polybot live | dry_run={CONFIG.dry_run} | cap={CONFIG.max_trades_per_day}/day "
              f"| resuming {len(store.all())} open trade(s)")

        while True:
            for sig in monitor.poll_once():
                _handle_signal(sig, pm, executor, store, anthropic)
            _handle_exits(pm, executor, store)
            time.sleep(CONFIG.poll_seconds)


if __name__ == "__main__":
    run()
