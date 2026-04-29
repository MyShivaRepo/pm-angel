"""Open-Meteo client for weather forecasts (free, no API key)."""
from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=15)
CACHE_TTL_SECONDS = 1800  # 30 min


class OpenMeteoClient:
    """Async client for https://api.open-meteo.com/v1/forecast"""

    def __init__(self, base_url: str = "https://api.open-meteo.com"):
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=DEFAULT_TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_daily(
        self,
        lat: float,
        lon: float,
        target_date: date,
        variables: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch daily forecast for a single date.

        Returns a dict with the requested variables. Each variable is a list of values
        (one per day in the date range — here, always one value since start=end).
        """
        if variables is None:
            variables = [
                "precipitation_probability_max",
                "precipitation_sum",
                "snowfall_sum",
                "temperature_2m_max",
                "temperature_2m_min",
            ]

        cache_key = (round(lat, 3), round(lon, 3), target_date.isoformat(), tuple(variables))

        # Cache hit
        import time as _time
        now = _time.time()
        cached = self._cache.get(cache_key)
        if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
            return cached[1]

        async with self._lock:
            # Re-check after acquiring lock
            cached = self._cache.get(cache_key)
            if cached and (now - cached[0]) < CACHE_TTL_SECONDS:
                return cached[1]

            params = {
                "latitude": lat,
                "longitude": lon,
                "daily": ",".join(variables),
                "timezone": "UTC",
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
            }

            session = await self._get_session()
            url = f"{self._base_url}/v1/forecast"
            try:
                async with session.get(url, params=params) as resp:
                    resp.raise_for_status()
                    data = await resp.json()
            except aiohttp.ClientError as exc:
                logger.error("Open-Meteo request failed: lat=%s lon=%s date=%s: %s", lat, lon, target_date, exc)
                raise

            daily = data.get("daily", {})
            self._cache[cache_key] = (now, daily)
            return daily
