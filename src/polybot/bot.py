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


def _held_shares(pm: PolymarketClient, token_id: str) -> float:
    """Shares of ``token_id`` actually held by our funder — post-fill ground truth."""
    try:
        for p in pm.positions(CONFIG.funder):
            if str(p.get("asset", "")) == token_id:
                return float(p.get("size", 0) or 0)
    except Exception:
        pass
    return 0.0


def _handle_signal(sig: Signal, pm: PolymarketClient, executor: Executor,
                   store: TradeStore) -> None:
    if store.has(sig.market_id):
        return  # already holding this market

    print(f"\n🔔 SIGNAL {sig.title} | {sig.outcome} @ {sig.avg_price} "
          f"| {len(sig.wallets)} wallets")

    market = pm.market(sig.market_id)
    if not isinstance(market, dict) or not market:
        # Gamma returns [] for archived/unknown markets (e.g. long-resolved ones)
        print(f"   skip: no market metadata for {sig.market_id}")
        return

    vote = vote_on_signal(sig, market)  # web-grounded gate (Claude Code / API)
    print(f"   Claude: {'ENTER' if vote.enter else 'SKIP'} "
          f"({vote.confidence:.0%}) — {vote.reason}")
    if not vote.enter:
        return

    token_id = pm.token_id_for(market, sig.outcome)
    if not token_id:
        print(f"   skip: could not resolve token_id for outcome '{sig.outcome}'")
        return

    # Price off the live CLOB midpoint — Gamma's outcomePrices can be badly stale.
    mid = pm.midpoint(token_id)
    price = mid if mid and mid > 0 else (pm.outcome_price(market, sig.outcome) or sig.avg_price)

    # Anti-chase: by the time consensus -> vote -> execute completes, the price may
    # have run away from where the smart wallets entered. Don't buy the top.
    if sig.avg_price > 0 and price > sig.avg_price * (1 + CONFIG.max_chase_pct):
        print(f"   skip: price {price:.3f} ran {((price / sig.avg_price) - 1) * 100:.0f}% "
              f"past consensus {sig.avg_price:.3f} (chasing)")
        return

    stake = CONFIG.stake_usd
    shares = stake / price if price > 0 else 0.0

    try:
        status = executor.place(Order(
            market_id=sig.market_id, outcome=sig.outcome, side="BUY",
            size_usd=stake, price=price, reason=f"consensus:{len(sig.wallets)}",
            token_id=token_id, shares=shares,
        ))
    except RiskError as e:
        print(f"   blocked: {e}")
        return

    # Record the ACTUAL filled size as ground truth (a market FOK can fill away
    # from the midpoint). Effective cost basis = stake / shares actually held, so
    # take-profit/stop-loss measure against what we really paid.
    entry_price, entry_shares = price, shares
    if status == "FILLED":
        held = _held_shares(pm, token_id)
        if held > 0:
            entry_shares = held
            entry_price = stake / held

    store.add(OpenTrade(
        market_id=sig.market_id, token_id=token_id, outcome=sig.outcome,
        title=sig.title, entry_price=entry_price, shares=entry_shares, stake_usd=stake,
        trigger_wallets=list(sig.wallets),
        baseline_size=_whale_position_sizes(pm, sig.market_id, sig.wallets),
    ))


def _handle_exits(pm: PolymarketClient, executor: Executor, store: TradeStore) -> None:
    for market_id, trade in store.all().items():
        # Price off the live CLOB midpoint (same as entries) — Gamma's
        # outcomePrices lag, which would stop take-profit/stop-loss from firing.
        mid = pm.midpoint(trade.token_id)
        if mid and mid > 0:
            current = mid
        else:
            market = pm.market(market_id)
            current = (pm.outcome_price(market, trade.outcome)
                       if isinstance(market, dict) and market else None) or trade.entry_price
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
    import sys
    # Line-buffer stdout so the log is live even when redirected to a file
    # (default block-buffering hides output until ~4KB accumulates).
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    wallets = load_top_wallets()
    store = TradeStore()

    with PolymarketClient() as pm:
        monitor = Monitor(wallets, pm)
        executor = Executor()
        print(f"polybot live | dry_run={CONFIG.dry_run} | cap={CONFIG.max_trades_per_day}/day "
              f"| resuming {len(store.all())} open trade(s)")
        # heartbeat so 'is it alive?' is answerable from the log
        import datetime as _dt
        polls = 0

        while True:
            try:
                sigs = monitor.poll_once()
                for sig in sigs:
                    _handle_signal(sig, pm, executor, store)
                _handle_exits(pm, executor, store)
                polls += 1
                if polls % 10 == 0:  # ~every 10 cycles
                    print(f"  [{_dt.datetime.now():%H:%M:%S}] alive — {polls} polls, "
                          f"{len(store.all())} open")
            except Exception as exc:  # never let one bad cycle kill the loop
                print(f"  ! cycle error (continuing): {exc}")
            time.sleep(CONFIG.poll_seconds)


if __name__ == "__main__":
    run()
