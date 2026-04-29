from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class ClobWrapper:
    """Thin async wrapper around py-clob-client (synchronous SDK)."""

    def __init__(
        self,
        host: str,
        private_key: str,
        chain_id: int,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        funder: str = "",
    ):
        self._host = host
        self._private_key = private_key
        self._chain_id = chain_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase
        self._funder = funder
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client

        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        kwargs = dict(
            host=self._host,
            key=self._private_key,
            chain_id=self._chain_id,
            signature_type=1,  # POLY_PROXY for Magic Link email accounts
            creds=ApiCreds(
                api_key=self._api_key,
                api_secret=self._api_secret,
                api_passphrase=self._api_passphrase,
            ),
        )
        if self._funder:
            kwargs["funder"] = self._funder

        self._client = ClobClient(**kwargs)
        return self._client

    async def derive_api_creds(self, funder: str = "") -> dict[str, str]:
        """Derive API credentials from private key."""
        from py_clob_client.client import ClobClient

        kwargs = dict(
            host=self._host,
            key=self._private_key,
            chain_id=self._chain_id,
            signature_type=1,
        )
        if funder:
            kwargs["funder"] = funder

        client = ClobClient(**kwargs)
        creds = await asyncio.to_thread(client.create_or_derive_api_creds)
        return {
            "api_key": creds.api_key,
            "api_secret": creds.api_secret,
            "api_passphrase": creds.api_passphrase,
        }

    async def get_ok(self) -> bool:
        try:
            client = self._ensure_client()
            result = await asyncio.to_thread(client.get_ok)
            return result == "OK"
        except Exception as exc:
            logger.error("CLOB health check failed: %s", exc)
            return False

    async def get_balance(self) -> float:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

        client = self._ensure_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=1)
        result = await asyncio.to_thread(client.get_balance_allowance, params)
        return float(result.get("balance", 0)) / 1e6

    async def get_midpoint(self, token_id: str) -> float:
        client = self._ensure_client()
        result = await asyncio.to_thread(client.get_midpoint, token_id)
        if isinstance(result, dict):
            return float(result.get("mid", 0))
        return float(result)

    async def place_market_order(
        self, token_id: str, amount_usd: float, side: str,
        neg_risk: bool | None = None,
    ) -> dict[str, Any]:
        from py_clob_client.clob_types import (
            MarketOrderArgs, OrderType, PartialCreateOrderOptions,
        )

        client = self._ensure_client()

        args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=side.upper(),
        )

        # Explicitly pass neg_risk when known (avoids SDK auto-detect failures
        # that surface as 'order_version_mismatch' on multi-outcome markets).
        options = PartialCreateOrderOptions(neg_risk=neg_risk) if neg_risk is not None else None

        signed = await asyncio.to_thread(client.create_market_order, args, options)
        result = await asyncio.to_thread(client.post_order, signed, OrderType.FOK)
        logger.info(
            "Market order placed: %s %s $%.2f neg_risk=%s -> %s",
            side, token_id[:16], amount_usd, neg_risk, result
        )
        return result

    async def place_limit_order(
        self, token_id: str, price: float, size: float, side: str
    ) -> dict[str, Any]:
        from py_clob_client.clob_types import OrderArgs, OrderType

        client = self._ensure_client()

        args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side.upper(),
        )

        signed = await asyncio.to_thread(client.create_order, args)
        result = await asyncio.to_thread(client.post_order, signed, OrderType.GTC)
        logger.info(
            "Limit order placed: %s %s @ %.4f x %.2f -> %s",
            side, token_id[:16], price, size, result
        )
        return result

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        client = self._ensure_client()
        return await asyncio.to_thread(client.cancel, order_id)

    async def get_positions(self) -> list[dict[str, Any]]:
        client = self._ensure_client()
        return await asyncio.to_thread(client.get_positions)
