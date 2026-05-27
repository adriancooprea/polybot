#!/usr/bin/env python3
"""Backtest the consensus strategy on the LOCAL historical snapshot (2022-2025).

Design (honest out-of-sample):
  RANK  wallets on realized PnL over a rank window (ground-truth resolutions,
        live filters: >=50 trades, >=60% win-rate, top 100 by profit).
  TRADE their consensus signals (>=N distinct top wallets BUY the same token
        within 30 min) over a *later* trade window -> no lookahead in ranking.
  EXIT  hold-to-resolution: settle each entered token at its ground-truth
        outcome (resolutions.csv). PnL = stake * (settle/entry - 1).

Caveats: snapshot is fills, not a quote feed -> only hold-to-resolution (no
intraday TP/SL). 2024-25 era, not 2026. No Claude vote (same lookahead reason).
"""
from __future__ import annotations

import datetime as dt
import statistics as st
from collections import defaultdict, deque
from pathlib import Path

import duckdb

DATA = Path(__file__).resolve().parent.parent / "data"
CSV = DATA / "orderFilled_complete.csv"
RES = DATA / "resolutions.csv"

# wide window persisted to parquet once; splits are slices of it
def _ts(y, m, d):
    return int(dt.datetime(y, m, d, tzinfo=dt.UTC).timestamp())

WIDE_START = _ts(2024, 1, 1)
WIDE_END = _ts(2025, 10, 5)
# walk-forward splits: (rank_start, rank_end, trade_start, trade_end)
SPLITS = [
    (_ts(2024, 1, 1), _ts(2024, 7, 1), _ts(2024, 7, 1), _ts(2025, 1, 1)),
    (_ts(2024, 7, 1), _ts(2025, 1, 1), _ts(2025, 1, 1), _ts(2025, 7, 1)),
    (_ts(2025, 1, 1), _ts(2025, 7, 1), _ts(2025, 7, 1), _ts(2025, 10, 5)),
]

# strategy params (mirror live)
MIN_AGREE = 3
WINDOW_S = 30 * 60
NEAR_LO, NEAR_HI = 0.05, 0.95
MAX_PER_DAY = 10
STAKE = 2.0
TOP_N = 100

_COLS = ("{'timestamp':'BIGINT','maker':'VARCHAR','makerAssetId':'VARCHAR',"
         "'makerAmountFilled':'DOUBLE','taker':'VARCHAR','takerAssetId':'VARCHAR',"
         "'takerAmountFilled':'DOUBLE','transactionHash':'VARCHAR'}")
_READ = f"read_csv('{CSV}', header=true, columns={_COLS})"

PARQUET = DATA / "backtest_cache" / "fills_2024_2025.parquet"

NORM = f"""
COPY (
  WITH raw AS (
    SELECT * FROM {_READ}
    WHERE timestamp >= {WIDE_START} AND timestamp < {WIDE_END}
      AND (makerAssetId = '0' OR takerAssetId = '0')
  )
  SELECT timestamp AS ts,
    CASE WHEN makerAssetId='0' THEN takerAssetId ELSE makerAssetId END AS token,
    CASE WHEN makerAssetId='0' THEN maker ELSE taker END AS buyer,
    CASE WHEN makerAssetId='0' THEN taker ELSE maker END AS seller,
    (CASE WHEN makerAssetId='0' THEN makerAmountFilled ELSE takerAmountFilled END)/1e6 AS usd,
    (CASE WHEN makerAssetId='0' THEN takerAmountFilled ELSE makerAmountFilled END)/1e6 AS tokens
  FROM raw
) TO '{PARQUET}' (FORMAT parquet);
"""


def rank_wallets(con, rank_end):
    sql = f"""
    WITH truth AS (
      SELECT CAST(token_id AS VARCHAR) token, CAST(settlement AS DOUBLE) settle
      FROM read_csv_auto('{RES}', header=true)),
    lastp AS (SELECT token, arg_max(usd/tokens, ts) fp FROM fills WHERE tokens>0 GROUP BY token),
    settle AS (SELECT lp.token, COALESCE(t.settle, CASE WHEN lp.fp>={NEAR_HI} THEN 1.0
                                                        WHEN lp.fp<={NEAR_LO} THEN 0.0 END) settle
               FROM lastp lp LEFT JOIN truth t USING(token)),
    rk AS (SELECT * FROM fills WHERE ts < {rank_end} AND tokens>0),
    legs AS (SELECT buyer wallet, token, tokens tk, -usd usd FROM rk
             UNION ALL SELECT seller wallet, token, -tokens tk, usd usd FROM rk),
    pos AS (SELECT wallet, token, sum(tk) net_tk, sum(usd) net_usd, count(*) nlegs
            FROM legs GROUP BY wallet, token),
    pnl AS (SELECT p.wallet, p.nlegs, p.net_usd + p.net_tk*s.settle profit
            FROM pos p JOIN settle s USING(token) WHERE s.settle IS NOT NULL),
    ws AS (SELECT wallet, sum(nlegs) trades, sum(profit) tot,
                  avg(CASE WHEN profit>0 THEN 1.0 ELSE 0.0 END) wr FROM pnl GROUP BY wallet)
    SELECT wallet FROM ws WHERE trades>=50 AND wr>=0.60 ORDER BY tot DESC LIMIT {TOP_N};
    """
    return [r[0] for r in con.execute(sql).fetchall()]


def signals_for(con, top, trade_start, trade_end):
    con.execute("CREATE OR REPLACE TEMP TABLE topw AS SELECT UNNEST(?) AS w", [top])
    buys = con.execute(f"""
        SELECT ts, token, buyer, usd/tokens price FROM fills
        WHERE ts>={trade_start} AND ts<{trade_end} AND tokens>0 AND usd>0
          AND buyer IN (SELECT w FROM topw) ORDER BY ts""").fetchall()
    windows, fired, signals = defaultdict(deque), set(), []
    for ts, token, buyer, price in buys:
        win = windows[token]
        win.append((ts, buyer, price))
        while win and win[0][0] < ts - WINDOW_S:
            win.popleft()
        wset = {b for _, b, _ in win}
        if len(wset) < MIN_AGREE:
            continue
        key = (token, frozenset(wset))
        if key in fired:
            continue
        fired.add(key)
        prices = [p for _, _, p in win if p]
        signals.append((ts, token, sum(prices)/len(prices) if prices else 0.0, len(wset)))
    return len(buys), signals


def run_strategy(signals, res, entry_lo, entry_hi):
    per_day, entered, trades = defaultdict(int), set(), []
    for ts, token, entry, nw in sorted(signals):
        if token in entered or entry <= 0:
            continue
        if entry >= NEAR_HI or entry <= NEAR_LO or not (entry_lo <= entry <= entry_hi):
            continue
        day = dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%d")
        if per_day[day] >= MAX_PER_DAY or token not in res:
            continue
        per_day[day] += 1
        entered.add(token)
        trades.append({"entry": entry, "settle": res[token],
                       "pnl": STAKE * (res[token]/entry - 1)})
    return trades


def summ(trades):
    n = len(trades)
    if not n:
        return "  (no trades)"
    pnls = [t["pnl"] for t in trades]
    tot = sum(pnls)
    w = sum(1 for t in trades if t["settle"] >= 0.5)
    return (f"  {n:3} trades | win {w/n*100:3.0f}% | ROI {tot/(n*STAKE)*100:+6.1f}% | "
            f"PnL ${tot:+7.2f} | ex-top ${tot-max(pnls):+7.2f}")


def main():
    con = duckdb.connect()
    con.execute("PRAGMA threads=4; PRAGMA memory_limit='6GB';")
    if not PARQUET.exists():
        print("scanning 37GB snapshot -> parquet (one-time, 2024-2025)…", flush=True)
        con.execute(NORM)
    con.execute(f"CREATE OR REPLACE VIEW fills AS SELECT * FROM read_parquet('{PARQUET}')")
    print(f"fills: {con.execute('SELECT count(*) FROM fills').fetchone()[0]:,}", flush=True)
    res = {tok: s for tok, s in con.execute(f"""
        WITH truth AS (SELECT CAST(token_id AS VARCHAR) token, CAST(settlement AS DOUBLE) settle
                       FROM read_csv_auto('{RES}', header=true)),
             lastp AS (SELECT token, arg_max(usd/tokens, ts) fp FROM fills WHERE tokens>0 GROUP BY token)
        SELECT lp.token, COALESCE(t.settle, CASE WHEN lp.fp>={NEAR_HI} THEN 1.0
                                                 WHEN lp.fp<={NEAR_LO} THEN 0.0 END)
        FROM lastp lp LEFT JOIN truth t USING(token)""").fetchall() if s is not None}

    filters = {"baseline (all)": (0.0, 1.0), "favorites >=0.50": (0.50, 1.0),
               "favorites >=0.65": (0.65, 1.0), "favorites 0.50-0.90": (0.50, 0.90)}
    agg = defaultdict(list)
    for rs, re_, ts0, te in SPLITS:
        top = rank_wallets(con, re_)
        nbuys, sigs = signals_for(con, top, ts0, te)
        lbl = (f"rank {dt.datetime.fromtimestamp(rs,dt.UTC):%Y-%m}.."
               f"{dt.datetime.fromtimestamp(re_,dt.UTC):%Y-%m} -> trade "
               f"{dt.datetime.fromtimestamp(ts0,dt.UTC):%Y-%m}..{dt.datetime.fromtimestamp(te,dt.UTC):%Y-%m}")
        print(f"\n### {lbl}  ({len(top)} wallets, {nbuys:,} buys, {len(sigs)} raw signals)")
        for fname, (lo, hi) in filters.items():
            tr = run_strategy(sigs, res, lo, hi)
            print(f" {fname:22}{summ(tr)}")
            agg[fname].append(tr)

    print("\n===== AGGREGATE ACROSS ALL 3 OOS SPLITS =====")
    for fname in filters:
        allt = [t for tr in agg[fname] for t in tr]
        print(f" {fname:22}{summ(allt)}")


if __name__ == "__main__":
    main()
