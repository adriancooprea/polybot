"""Rank Polymarket wallets by win rate, profit, and consistency.

Runs over the raw ``orderFilled`` snapshot from poly_data (~86M rows) with
DuckDB (out-of-core SQL — the CSV does not fit in RAM). Produces the top-N
"smart money" wallets that become our signal sources downstream.

orderFilled schema
------------------
``timestamp, maker, makerAssetId, makerAmountFilled, taker, takerAssetId,
takerAmountFilled, transactionHash``

Each row is one on-chain fill. One side of the swap is USDC (``assetId = '0'``),
the other is an outcome token. The party paying USDC is **buying** the token;
the party giving the token is **selling**. Amounts are 6-decimal fixed point.
Token ids are 77-digit integers, so they are read as VARCHAR.

Pipeline
--------
1. Normalize each fill into (token, buyer, seller, usd, tokens).
2. Explode into signed per-wallet legs (buyer: +tokens/-usd, seller: -tokens/+usd).
3. Net each (wallet, token) position; settle at resolution to get PnL.
4. Aggregate per wallet: win rate, total profit, trade count, profit factor.
5. Filter (min trades, min win rate, recency window) and take the top N.

Resolution
----------
Settlement = 1.0 for the winning token, 0.0 for the loser. By default we *infer*
it from the token's final observed price (near 1 => win, near 0 => loss; markets
still mid-range are excluded from PnL). Pass ``resolutions`` (token_id,settlement)
to override with ground truth — token_id there matches the on-chain assetId.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

DATA_DIR = Path(__file__).resolve().parents[3] / "data"
DEFAULT_TRADES = DATA_DIR / "orderFilled_complete.csv"

WIN_PRICE = 0.95  # final price >= this => token settled to 1
LOSE_PRICE = 0.05  # final price <= this => token settled to 0

_READ = (
    "read_csv('{path}', header=true, columns={{"
    "'timestamp':'BIGINT','maker':'VARCHAR','makerAssetId':'VARCHAR',"
    "'makerAmountFilled':'DOUBLE','taker':'VARCHAR','takerAssetId':'VARCHAR',"
    "'takerAmountFilled':'DOUBLE','transactionHash':'VARCHAR'}})"
)


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
    read = _READ.format(path=trades_csv)

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
    LEFT JOIN truth t USING (token)
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
WITH raw AS (
    SELECT * FROM {read}
    WHERE makerAssetId = '0' OR takerAssetId = '0'   -- one leg must be USDC
),
-- normalize every fill: who bought the token, who sold, for how much
fills_all AS (
    SELECT timestamp,
           CASE WHEN makerAssetId = '0' THEN takerAssetId ELSE makerAssetId END AS token,
           CASE WHEN makerAssetId = '0' THEN maker ELSE taker END AS buyer,
           CASE WHEN makerAssetId = '0' THEN taker ELSE maker END AS seller,
           (CASE WHEN makerAssetId = '0' THEN makerAmountFilled ELSE takerAmountFilled END)
               / 1e6 AS usd,
           (CASE WHEN makerAssetId = '0' THEN takerAmountFilled ELSE makerAmountFilled END)
               / 1e6 AS tokens
    FROM raw
),
-- final observed price per token, over ALL history (for settlement inference)
last_price AS (
    SELECT token, arg_max(usd / tokens, timestamp) AS final_price
    FROM fills_all
    WHERE tokens > 0
    GROUP BY token
),{settlement_cte}
-- recency window anchored to newest fill (deterministic vs a static snapshot)
bounds AS (SELECT max(timestamp) AS max_ts FROM fills_all),
fills AS (
    SELECT f.* FROM fills_all f, bounds b
    WHERE f.timestamp >= b.max_ts - CAST({window_days} AS BIGINT) * 86400
      AND f.tokens > 0
),
-- signed per-wallet legs
legs AS (
    SELECT buyer AS wallet, token, tokens AS tokens, -usd AS usd FROM fills
    UNION ALL
    SELECT seller AS wallet, token, -tokens AS tokens, usd AS usd FROM fills
),
positions AS (
    SELECT wallet, token,
           sum(tokens) AS net_tokens,
           sum(usd) AS net_usd,
           count(*) AS n_legs
    FROM legs
    GROUP BY wallet, token
),
pnl AS (
    SELECT p.wallet, p.n_legs,
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
    # Materialize once into a temp table — the query is a full pass over ~151M
    # rows; running it twice (once to fetch, once to COPY) doubles the work.
    con.execute(f"CREATE TEMP TABLE ranked AS {sql.rstrip().rstrip(';')}")
    if out_csv is not None:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        con.execute(f"COPY ranked TO '{out_csv}' (HEADER, DELIMITER ',')")
        print(f"Wrote ranked wallets to {out_csv}")
    return con.execute("SELECT * FROM ranked")


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank Polymarket wallets by performance.")
    ap.add_argument("--trades", type=Path, default=DEFAULT_TRADES,
                    help="orderFilled_complete.csv from `polybot download`")
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
