"""Rank Polymarket wallets by win rate, profit, and consistency.

Runs over the ~86M-row poly_data snapshot with DuckDB (out-of-core SQL — the
CSV does not fit in RAM). Produces the top-N "smart money" wallets that become
our signal sources downstream.

Pipeline
--------
1. Explode each trade into per-participant rows: every trade has a maker and a
   taker, each with a BUY/SELL direction on an outcome token.
2. Net each (wallet, market) position: tokens held and USD flow.
3. Settle each position against the market's resolved outcome to get PnL.
4. Aggregate per wallet: win rate, total profit, trade count, profit factor.
5. Filter (min trades, min win rate, recency window) and take the top N.

Resolution caveat
-----------------
The raw dataset does not carry an explicit "winning outcome" flag. We *infer*
settlement: a token whose final observed trade price converged near 1.0 is
treated as a winner (value 1), near 0.0 as a loser (value 0); anything in
between (still-open / ambiguous markets) is excluded from PnL. Pass a
``resolutions`` CSV (token_id,settlement) to override the heuristic with ground
truth when you have it.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DEFAULT_TRADES = DATA_DIR / "processed" / "trades.csv"

WIN_PRICE = 0.95  # final price >= this => token settled to 1
LOSE_PRICE = 0.05  # final price <= this => token settled to 0


def build_sql(
    trades_csv: Path,
    *,
    min_trades: int,
    min_win_rate: float,
    window_days: int,
    top_n: int,
    resolutions_csv: Path | None = None,
) -> str:
    """Return the DuckDB SQL that produces the ranked wallet table.

    If ``resolutions_csv`` is given, ground-truth settlement overrides the
    inferred final-price heuristic for any token present in that file.
    """
    if resolutions_csv is not None:
        settlement_cte = f"""
truth AS (
    SELECT CAST(token_id AS VARCHAR) AS token, CAST(settlement AS DOUBLE) AS settle
    FROM read_csv_auto('{resolutions_csv}', header=true)
),
settlement AS (
    SELECT lp.token,
           COALESCE(t.settle,
                    CASE WHEN lp.final_price >= {WIN_PRICE} THEN 1.0
                         WHEN lp.final_price <= {LOSE_PRICE} THEN 0.0
                         ELSE NULL END) AS settle
    FROM last_price lp
    LEFT JOIN truth t ON CAST(lp.token AS VARCHAR) = t.token
),"""
    else:
        settlement_cte = f"""
settlement AS (
    SELECT token,
           CASE WHEN final_price >= {WIN_PRICE} THEN 1.0
                WHEN final_price <= {LOSE_PRICE} THEN 0.0
                ELSE NULL END AS settle
    FROM last_price
),"""
    return f"""
WITH trades AS (
    SELECT * FROM read_csv_auto('{trades_csv}', header=true)
),
-- recency: anchor the window to the newest trade in the data, not wall-clock,
-- so the ranker is deterministic against a static snapshot.
bounds AS (
    SELECT max(timestamp) AS max_ts FROM trades
),
recent AS (
    SELECT t.*
    FROM trades t, bounds b
    WHERE t.timestamp >= b.max_ts - CAST({window_days} AS BIGINT) * 86400
),
-- one row per (wallet, token) per trade, signed by direction
legs AS (
    SELECT maker AS wallet, market_id, makerAssetId AS token, timestamp,
           CASE WHEN maker_direction = 'BUY' THEN token_amount ELSE -token_amount END AS tokens,
           CASE WHEN maker_direction = 'BUY' THEN -usd_amount ELSE usd_amount END AS usd
    FROM recent
    UNION ALL
    SELECT taker AS wallet, market_id, takerAssetId AS token, timestamp,
           CASE WHEN taker_direction = 'BUY' THEN token_amount ELSE -token_amount END AS tokens,
           CASE WHEN taker_direction = 'BUY' THEN -usd_amount ELSE usd_amount END AS usd
    FROM recent
),
-- infer settlement per token from its final observed price
last_price AS (
    SELECT token, arg_max(price, timestamp) AS final_price
    FROM (
        SELECT makerAssetId AS token, price, timestamp FROM trades
        UNION ALL
        SELECT takerAssetId AS token, price, timestamp FROM trades
    )
    GROUP BY token
),{settlement_cte}
-- net each wallet's position per token, then realize PnL at settlement
positions AS (
    SELECT l.wallet, l.market_id, l.token,
           sum(l.tokens) AS net_tokens,
           sum(l.usd) AS net_usd,
           count(*) AS n_legs
    FROM legs l
    GROUP BY l.wallet, l.market_id, l.token
),
pnl AS (
    SELECT p.wallet, p.market_id, p.n_legs,
           p.net_usd + p.net_tokens * s.settle AS profit
    FROM positions p
    JOIN settlement s USING (token)
    WHERE s.settle IS NOT NULL          -- only resolved markets count
),
wallet_stats AS (
    SELECT wallet,
           count(*) AS positions,
           sum(n_legs) AS trades,
           sum(profit) AS total_profit,
           avg(CASE WHEN profit > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
           sum(CASE WHEN profit > 0 THEN profit ELSE 0 END) AS gross_win,
           -sum(CASE WHEN profit < 0 THEN profit ELSE 0 END) AS gross_loss,
           stddev_pop(profit) AS profit_std
    FROM pnl
    GROUP BY wallet
)
SELECT wallet,
       trades,
       positions,
       round(win_rate, 4) AS win_rate,
       round(total_profit, 2) AS total_profit,
       round(CASE WHEN gross_loss = 0 THEN NULL ELSE gross_win / gross_loss END, 3) AS profit_factor,
       round(profit_std, 2) AS profit_std
FROM wallet_stats
WHERE trades >= {min_trades}
  AND win_rate >= {min_win_rate}
ORDER BY total_profit DESC
LIMIT {top_n};
"""


def rank_wallets(
    trades_csv: Path = DEFAULT_TRADES,
    *,
    min_trades: int = 50,
    min_win_rate: float = 0.60,
    window_days: int = 90,
    top_n: int = 100,
    out_csv: Path | None = None,
    resolutions_csv: Path | None = None,
):
    """Run the ranking query and return the result as a DuckDB relation."""
    sql = build_sql(
        trades_csv,
        min_trades=min_trades,
        min_win_rate=min_win_rate,
        window_days=window_days,
        top_n=top_n,
        resolutions_csv=resolutions_csv,
    )
    con = duckdb.connect()
    rel = con.execute(sql)
    if out_csv is not None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ({sql.rstrip().rstrip(';')}) TO '{out_csv}' (HEADER, DELIMITER ',')")
        print(f"Wrote ranked wallets to {out_csv}")
    return rel


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank Polymarket wallets by performance.")
    ap.add_argument("--trades", type=Path, default=DEFAULT_TRADES, help="processed trades.csv")
    ap.add_argument("--min-trades", type=int, default=50)
    ap.add_argument("--min-win-rate", type=float, default=0.60)
    ap.add_argument("--window-days", type=int, default=90)
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--out", type=Path, default=DATA_DIR / "top_wallets.csv")
    ap.add_argument("--resolutions", type=Path, default=None,
                    help="resolutions.csv (token_id,settlement) for ground-truth PnL")
    args = ap.parse_args()

    resolutions = args.resolutions
    if resolutions is None:
        default_res = DATA_DIR / "resolutions.csv"
        resolutions = default_res if default_res.exists() else None

    rel = rank_wallets(
        args.trades,
        min_trades=args.min_trades,
        min_win_rate=args.min_win_rate,
        window_days=args.window_days,
        top_n=args.top,
        out_csv=args.out,
        resolutions_csv=resolutions,
    )
    cols = [d[0] for d in rel.description]
    print("  ".join(cols))
    for row in rel.fetchall():
        print("  ".join(str(v) for v in row))


if __name__ == "__main__":
    main()
