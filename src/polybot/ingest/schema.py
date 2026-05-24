"""Column schemas for the poly_data dataset.

Source: https://github.com/warproxxx/poly_data
"""

from __future__ import annotations

# markets.csv — market metadata
MARKETS_COLUMNS = (
    "createdAt",
    "id",
    "question",
    "answer1",
    "answer2",
    "neg_risk",
    "market_slug",
    "token1",
    "token2",
    "condition_id",
    "volume",
    "ticker",
    "closedTime",
)

# goldsky/orderFilled.csv — raw on-chain order-fill events (the big snapshot)
ORDER_FILLED_COLUMNS = (
    "timestamp",
    "maker",
    "makerAssetId",
    "makerAmountFilled",
    "taker",
    "takerAssetId",
    "takerAmountFilled",
    "transactionHash",
)

# processed/trades.csv — structured trades (preferred for ranking)
TRADES_COLUMNS = (
    "timestamp",
    "market_id",
    "maker",
    "taker",
    "nonusdc_side",
    "maker_direction",
    "taker_direction",
    "price",
    "usd_amount",
    "token_amount",
    "transactionHash",
)
