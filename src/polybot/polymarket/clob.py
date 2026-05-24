"""Live trade execution against the Polymarket CLOB (V2).

Wraps ``py-clob-client-v2``. Polymarket migrated to CLOB V2 on 2026-04-28; the
legacy ``py-clob-client`` no longer works against production (every order
returns ``order_version_mismatch``).

Constructed lazily — only when the bot is armed (``DRY_RUN=false``) — so dry-run
and read-only paths never need wallet keys or the heavy client.

KNOWN LIMITATION (2026-05): for the new EIP-7702 *deposit wallets*
(``signature_type=3`` / POLY_1271), the SDK's L1 auth binds the API key to the
EOA instead of the deposit wallet, so order POSTs are rejected ("maker address
not allowed" / "Could not create api key"). Balance/allowance reads still work.
Tracked upstream: py-clob-client-v2 issues #65/#70/#71. Until fixed, such
accounts must trade manually; everything else in polybot runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import CONFIG

HOST = "https://clob.polymarket.com"


@dataclass(frozen=True)
class Fill:
    ok: bool
    order_id: str
    status: str
    raw: dict


class TradeClient:
    """Authenticated CLOB V2 client for placing market orders."""

    def __init__(self) -> None:
        if not CONFIG.wallet_private_key:
            raise RuntimeError("POLYMARKET_WALLET_PRIVATE_KEY required for live trading")
        from py_clob_client_v2 import ApiCreds, ClobClient

        kw = dict(host=HOST, chain_id=CONFIG.chain_id, key=CONFIG.wallet_private_key,
                  signature_type=CONFIG.signature_type)
        if CONFIG.funder:
            kw["funder"] = CONFIG.funder
        client = ClobClient(**kw)

        if CONFIG.polymarket_api_key and CONFIG.polymarket_api_secret and CONFIG.polymarket_passphrase:
            # Pre-supplied L2 creds (e.g. the website's deposit-wallet-bound key).
            # For sig_type=3 deposit wallets the L2 POLY_ADDRESS header must be the
            # deposit wallet (the key is bound to it, and orders set signer=funder),
            # so bind the signer's reported address to the funder. The EOA still
            # signs orders/HMAC — only the advertised address changes.
            # Works around py-clob-client-v2 #65/#70/#71 (can't mint a deposit-wallet
            # key via the SDK; we reuse one that already exists).
            if CONFIG.signature_type == 3 and CONFIG.funder:
                funder = CONFIG.funder
                client.signer.address = lambda: funder  # noqa: E731
            client.set_api_creds(ApiCreds(
                api_key=CONFIG.polymarket_api_key,
                api_secret=CONFIG.polymarket_api_secret,
                api_passphrase=CONFIG.polymarket_passphrase,
            ))
        else:
            # No creds supplied: derive them (works for EOA wallets; for sig_type=3
            # deposit wallets this binds to the EOA and order POSTs are rejected).
            client.set_api_creds(client.create_or_derive_api_key())
        self._client = client

    def _tick(self, token_id: str) -> str:
        try:
            return str(self._client.get_tick_size(token_id))
        except Exception:
            return "0.01"

    def market_buy(self, token_id: str, usd_amount: float) -> Fill:
        """Buy ``usd_amount`` USDC worth of ``token_id`` at market (FOK)."""
        from py_clob_client_v2 import MarketOrderArgsV2, OrderType, PartialCreateOrderOptions, Side

        resp = self._client.create_and_post_market_order(
            order_args=MarketOrderArgsV2(token_id=token_id, amount=float(usd_amount),
                                         side=Side.BUY, order_type=OrderType.FOK),
            options=PartialCreateOrderOptions(tick_size=self._tick(token_id)),
            order_type=OrderType.FOK,
        )
        return self._to_fill(resp)

    def market_sell(self, token_id: str, shares: float) -> Fill:
        """Sell ``shares`` of ``token_id`` at market (FOK)."""
        from py_clob_client_v2 import MarketOrderArgsV2, OrderType, PartialCreateOrderOptions, Side

        resp = self._client.create_and_post_market_order(
            order_args=MarketOrderArgsV2(token_id=token_id, amount=float(shares),
                                         side=Side.SELL, order_type=OrderType.FOK),
            options=PartialCreateOrderOptions(tick_size=self._tick(token_id)),
            order_type=OrderType.FOK,
        )
        return self._to_fill(resp)

    def collateral_balance(self) -> dict:
        """USDC balance/allowance for the funder (6-decimal strings)."""
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        return self._client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))

    @staticmethod
    def _to_fill(resp: dict) -> Fill:
        resp = resp or {}
        status = str(resp.get("status", ""))
        ok = bool(resp.get("success", status in {"matched", "live"}))
        return Fill(ok=ok, order_id=str(resp.get("orderID", "")), status=status, raw=resp)
