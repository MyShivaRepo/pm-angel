from __future__ import annotations

import logging
from typing import Any

from pm_angel.api.data_api import DataApiClient
from pm_angel.api.gamma_api import GammaApiClient
from pm_angel.schemas import LeaderboardEntry

logger = logging.getLogger(__name__)


class TraderDiscovery:
    """Discover and rank top Polymarket traders from the leaderboard."""

    def __init__(self, data_api: DataApiClient, gamma_api: GammaApiClient):
        self._data_api = data_api
        self._gamma_api = gamma_api

    async def get_top_traders(
        self,
        category: str = "OVERALL",
        time_period: str = "MONTH",
        order_by: str = "PNL",
        limit: int = 25,
        min_pnl: float = 0,
        min_volume: float = 0,
    ) -> list[LeaderboardEntry]:
        raw = await self._data_api.get_leaderboard(
            category=category,
            time_period=time_period,
            order_by=order_by,
            limit=limit,
        )

        traders = []
        for i, entry in enumerate(raw):
            pnl = float(entry.get("pnl", 0))
            volume = float(entry.get("volume", 0))
            if pnl < min_pnl or volume < min_volume:
                continue
            traders.append(
                LeaderboardEntry(
                    address=entry.get("userAddress", entry.get("address", "")),
                    username=entry.get("username", entry.get("userName", "")),
                    pnl=pnl,
                    volume=volume,
                    rank=i + 1,
                )
            )
        return traders

    async def get_trader_info(self, address: str) -> dict[str, Any]:
        profile = await self._gamma_api.get_public_profile(address)
        positions = await self._data_api.get_positions(address)
        trades = await self._data_api.get_trades(address, limit=50)

        wins = sum(1 for t in trades if float(t.get("pnl", 0)) > 0)
        total = len(trades) or 1

        return {
            "profile": profile,
            "positions_count": len(positions),
            "recent_trades": len(trades),
            "win_rate": wins / total,
        }
