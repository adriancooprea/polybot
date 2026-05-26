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

from . import journal
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


def _reconcile_positions(pm: PolymarketClient, store: TradeStore) -> None:
    """Sync tracked state to the actual on-chain holdings, both directions.

    ADOPT: a market order can return "delayed"/success yet only fill *after*
    ``_handle_signal`` gives up confirming it — leaving an orphan position with
    no exit management. Bring any held-but-untracked position under management.
    (Also re-attaches positions if state is ever lost.)

    PRUNE: drop tracked positions we no longer hold on-chain (sold out-of-band,
    or resolved/redeemed to zero). Necessary in hold-to-resolution mode: with
    the percentage exits disabled, ``_handle_exits`` no longer fires for these,
    so it never reaches the path that drops a zero-share position — they'd stay
    tracked forever.
    """
    try:
        positions = pm.positions(CONFIG.funder)
    except Exception as exc:
        print(f"  ! reconcile: positions fetch failed ({exc})")
        return
    held = {str(p.get("conditionId", "")) for p in positions
            if float(p.get("size", 0) or 0) > 0}
    for market_id, trade in store.all().items():
        if market_id not in held:
            store.remove(market_id)
            print(f"   pruned closed/resolved position: {trade.title[:40]} | {trade.outcome}")
    for p in positions:
        market_id = str(p.get("conditionId", ""))
        size = float(p.get("size", 0) or 0)
        if not market_id or size <= 0 or store.has(market_id):
            continue
        cur = float(p.get("curPrice", 0) or 0)
        # Resolved positions are pinned to 0/1 and redeem on their own — a winner
        # redeems at 1.0 (better than selling at 0.99), a loser is already gone.
        # Nothing to exit-manage, so don't adopt.
        if cur <= 0.05 or cur >= 0.95:
            continue
        entry = float(p.get("avgPrice", 0) or 0) or cur
        store.add(OpenTrade(
            market_id=market_id, token_id=str(p.get("asset", "")),
            outcome=str(p.get("outcome", "")), title=str(p.get("title", "")),
            entry_price=entry, shares=size,
            stake_usd=float(p.get("initialValue", 0) or 0) or entry * size,
            trigger_wallets=[], baseline_size={},
        ))
        print(f"   adopted untracked position: {str(p.get('title',''))[:40]} | "
              f"{p.get('outcome','')} {size}@{entry:.3f} (cur {cur:.3f})")


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

    token_id = pm.token_id_for(market, sig.outcome)
    if not token_id:
        print(f"   skip: could not resolve token_id for outcome '{sig.outcome}'")
        return

    # Price off the live CLOB midpoint — Gamma's outcomePrices can be badly stale.
    mid = pm.midpoint(token_id)
    price = mid if mid and mid > 0 else (pm.outcome_price(market, sig.outcome) or sig.avg_price)

    # CHEAP guards BEFORE the (slow, ~30s) web vote — don't spend a vote on a
    # signal the guards would reject anyway.
    # Near-resolved: market effectively decided, no edge.
    if price >= 0.95 or price <= 0.05:
        print(f"   skip: near-resolved (price {price:.3f}) — no vote")
        return
    # Anti-chase: price already ran past where the smart wallets entered.
    if sig.avg_price > 0 and price > sig.avg_price * (1 + CONFIG.max_chase_pct):
        print(f"   skip: price {price:.3f} ran {((price / sig.avg_price) - 1) * 100:.0f}% "
              f"past consensus {sig.avg_price:.3f} (chasing) — no vote")
        return

    vote = vote_on_signal(sig, market)  # web-grounded gate (Claude Code / API)
    print(f"   Claude: {'ENTER' if vote.enter else 'SKIP'} "
          f"({vote.confidence:.0%}) — {vote.reason}")
    journal.log_decision(sig, vote, market)  # record every vote (ENTER + SKIP) for later analysis
    if not vote.enter:
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

    # Confirm the fill actually happened before tracking. A market order can come
    # back "delayed"/success yet never match — tracking it would create a phantom
    # position. For live orders, require real shares held (brief retry for the
    # data-API lag); use the actual holdings as the cost basis. Dry-run keeps the
    # estimate.
    entry_price, entry_shares = price, shares
    if status == "FILLED":
        held = 0.0
        for _ in range(4):
            held = _held_shares(pm, token_id)
            if held > 0:
                break
            time.sleep(1.5)
        if held <= 0:
            print(f"   entry not confirmed (no shares held) — not tracking {sig.outcome}")
            return
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
        # Sell the ACTUAL held shares (floored), not the recorded count — a FAK
        # fill can leave slightly fewer shares than stake/price, and selling more
        # than held is rejected ("not enough balance").
        import math
        held = _held_shares(pm, trade.token_id)
        sell_shares = math.floor(held * 100) / 100.0
        if sell_shares <= 0:  # position already gone (dust / already sold)
            store.remove(market_id)
            continue
        try:
            executor.place(Order(
                market_id=market_id, outcome=trade.outcome, side="SELL",
                size_usd=sell_shares * current, price=current,
                reason=decision.reason, token_id=trade.token_id, shares=sell_shares,
            ))
            # Journal the ACTUAL fill price (thin books slip hard from the midpoint);
            # fall back to the midpoint only if we can't read the real fill.
            exit_px = executor.last_fill_price(trade.token_id, "SELL") or current
            pnl = journal.log_close(trade, exit_price=exit_px,
                                    exit_reason=decision.reason, shares_sold=sell_shares)
            print(f"   exit {trade.title[:32]} ({decision.reason}) — sold {sell_shares} @ {exit_px:.3f} | PnL ${pnl:+.2f}")
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
        # Adopt any orphan positions (delayed fills, lost state) before we start,
        # so they're exit-managed from the first cycle.
        _reconcile_positions(pm, store)
        print(f"polybot live | dry_run={CONFIG.dry_run} | cap={CONFIG.max_trades_per_day}/day "
              f"| resuming {len(store.all())} open trade(s)")
        # heartbeat so 'is it alive?' is answerable from the log
        import datetime as _dt
        polls = 0

        while True:
            try:
                # Catch any orphan fills (delayed market orders that landed after
                # _handle_signal gave up) and bring them under exit management.
                _reconcile_positions(pm, store)
                # Exits FIRST and BETWEEN signals — each ENTER vote takes ~30s, so
                # checking exits only after the whole signal batch starved them and
                # let profitable positions sit unlocked. Open positions are few, so
                # re-checking is cheap.
                _handle_exits(pm, executor, store)
                for sig in monitor.poll_once():
                    if store.all():
                        _handle_exits(pm, executor, store)
                    _handle_signal(sig, pm, executor, store)
                polls += 1
                if polls % 10 == 0:  # ~every 10 cycles
                    print(f"  [{_dt.datetime.now():%H:%M:%S}] alive — {polls} polls, "
                          f"{len(store.all())} open")
            except Exception as exc:  # never let one bad cycle kill the loop
                print(f"  ! cycle error (continuing): {exc}")
            time.sleep(CONFIG.poll_seconds)


if __name__ == "__main__":
    run()
