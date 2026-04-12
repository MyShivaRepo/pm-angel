from __future__ import annotations

import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)


class DataApiClient:
    """Async client for Polymarket Data API."""

    def __init__(self, base_url: str = "https://data-api.polymarket.com"):
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        try:
            async with session.get(url, params=params) as resp:
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as exc:
            logger.error("Data API request failed: %s %s -> %s", path, params, exc)
            raise

    async def get_leaderboard(
        self,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 25,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params = {
            "category": category,
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": min(limit, 50),
            "offset": offset,
        }
        return await self._get("/v1/leaderboard", params)

    async def get_activity(
        self,
        user: str,
        limit: int = 100,
        start_ts: int | None = None,
        activity_type: str = "TRADE",
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": user, "limit": limit}
        if activity_type:
            params["activityType"] = activity_type
        if start_ts is not None:
            params["start"] = start_ts
        return await self._get("/activity", params)

    async def get_positions(
        self, user: str, limit: int = 500
    ) -> list[dict[str, Any]]:
        return await self._get("/positions", {"user": user, "limit": limit})

    async def get_trades(
        self, user: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        return await self._get("/trades", {"user": user, "limit": limit})
