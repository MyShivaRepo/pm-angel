from __future__ import annotations

import logging
import time
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=30)
CACHE_TTL = 300  # 5 minutes


class GammaApiClient:
    """Async client for Polymarket Gamma API (market metadata)."""

    def __init__(self, base_url: str = "https://gamma-api.polymarket.com"):
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._market_cache: dict[str, tuple[float, dict]] = {}

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
            logger.error("Gamma API request failed: %s %s -> %s", path, params, exc)
            raise

    async def get_market(self, condition_id: str) -> dict[str, Any]:
        now = time.time()
        cached = self._market_cache.get(condition_id)
        if cached and (now - cached[0]) < CACHE_TTL:
            return cached[1]

        data = await self._get("/markets", {"condition_id": condition_id})
        if isinstance(data, list) and data:
            market = data[0]
        elif isinstance(data, dict):
            market = data
        else:
            raise ValueError(f"Market not found for condition_id={condition_id}")

        self._market_cache[condition_id] = (now, market)
        return market

    async def get_markets(
        self,
        active: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if active:
            params["active"] = "true"
        return await self._get("/markets", params)

    async def search_markets(self, query: str) -> dict[str, Any]:
        """Polymarket public search. Returns {events: [...], pagination: {...}}."""
        return await self._get("/public-search", {"q": query})

    async def get_weather_markets(self, limit: int = 200) -> list[dict[str, Any]]:
        """Discover active weather markets via /events endpoint.

        Polymarket weather markets are grouped under events like
        "Highest temperature in Seoul on April 17?" with multiple
        sub-markets (one per threshold). We list all active events
        and keep only those with a weather keyword in their title.
        """
        REQUIRED_KEYWORDS = (
            "temperature", " temp ", "hottest", "coldest", "warmest",
            "rain", "rainfall", "precipit",
            "snow", "snowfall",
            "weather",
        )

        results: dict[str, dict] = {}
        # Iterate /events with offsets to cover everything (active markets
        # change daily; cap at a few pages to keep things fast).
        for offset in (0, 500, 1000):
            try:
                params: dict[str, Any] = {
                    "active": "true",
                    "closed": "false",
                    "limit": 500,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "true",
                }
                data = await self._get("/events", params)
                if not isinstance(data, list) or not data:
                    break
                for ev in data:
                    if ev.get("closed") or ev.get("archived"):
                        continue
                    ev_title_lower = (ev.get("title") or "").lower()
                    if not any(kw in ev_title_lower for kw in REQUIRED_KEYWORDS):
                        continue
                    for m in ev.get("markets", []):
                        if m.get("closed") or m.get("archived"):
                            continue
                        cid = m.get("conditionId") or m.get("condition_id")
                        if not cid:
                            continue
                        m.setdefault("eventTitle", ev.get("title", ""))
                        m.setdefault("eventSlug", ev.get("slug", ""))
                        m.setdefault("endDate", m.get("endDate") or ev.get("endDate"))
                        results[cid] = m
                        if len(results) >= limit:
                            return list(results.values())
            except Exception as exc:
                logger.debug("get_weather_markets offset=%d failed: %s", offset, exc)
                break

        return list(results.values())

    async def get_public_profile(self, address: str) -> dict[str, Any]:
        return await self._get("/public-profile", {"address": address})

    def resolve_token_id(self, market: dict, outcome_index: int) -> str:
        """Extract the CLOB token ID from market data for a given outcome index."""
        clob_token_ids = market.get("clobTokenIds")
        if clob_token_ids:
            if isinstance(clob_token_ids, str):
                import json
                clob_token_ids = json.loads(clob_token_ids)
            if isinstance(clob_token_ids, list) and outcome_index < len(clob_token_ids):
                return clob_token_ids[outcome_index]
        tokens = market.get("tokens", [])
        if outcome_index < len(tokens):
            return tokens[outcome_index].get("token_id", "")
        raise ValueError(
            f"Cannot resolve token_id for outcome_index={outcome_index} "
            f"in market {market.get('condition_id')}"
        )
