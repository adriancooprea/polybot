"""Live trade execution against the Polymarket CLOB.

Wraps ``py-clob-client``. Constructed lazily — only when the bot is armed
(``DRY_RUN=false``) — so dry-run and read-only paths never need wallet keys or
the heavy client. All order placement flows through here.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CONFIG


@dataclass(frozen=True)
class Fill:
    ok: bool
    order_id: str
    status: str
    raw: dict


class TradeClient:
    """Authenticated CLOB client for placing market orders."""

    def __init__(self) -> None:
        if not CONFIG.wallet_private_key:
            raise RuntimeError("POLYMARKET_WALLET_PRIVATE_KEY required for live trading")
        # Imported here so dry-run never pays the import cost or needs the dep wired.
        from py_clob_client.client import ClobClient

        kwargs = dict(
            key=CONFIG.wallet_private_key,
            chain_id=CONFIG.chain_id,
            signature_type=CONFIG.signature_type,
        )
        if CONFIG.funder:
            kwargs["funder"] = CONFIG.funder
        self._client = ClobClient("https://clob.polymarket.com", **kwargs)
        self._client.set_api_creds(self._client.create_or_derive_api_creds())

    def market_buy(self, token_id: str, usd_amount: float) -> Fill:
        """Buy ``usd_amount`` worth of ``token_id`` at market (FOK)."""
        return self._market_order(token_id, usd_amount, "BUY")

    def market_sell(self, token_id: str, shares: float) -> Fill:
        """Sell ``shares`` of ``token_id`` at market (FOK)."""
        return self._market_order(token_id, shares, "SELL")

    def _market_order(self, token_id: str, amount: float, side: str) -> Fill:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        args = MarketOrderArgs(
            token_id=token_id,
            amount=float(amount),
            side=BUY if side == "BUY" else SELL,
            order_type=OrderType.FOK,
        )
        signed = self._client.create_market_order(args)
        resp = self._client.post_order(signed, OrderType.FOK)
        return Fill(
            ok=bool(resp.get("success", False)),
            order_id=str(resp.get("orderID", "")),
            status=str(resp.get("status", "")),
            raw=resp,
        )

    def midpoint(self, token_id: str) -> float | None:
        resp = self._client.get_midpoint(token_id)
        mid = resp.get("mid") if isinstance(resp, dict) else None
        return float(mid) if mid is not None else None
