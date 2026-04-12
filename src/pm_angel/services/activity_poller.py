from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pm_angel.api.data_api import DataApiClient
from pm_angel.api.gamma_api import GammaApiClient

logger = logging.getLogger(__name__)


@dataclass
class DetectedTrade:
    trader_address: str
    condition_id: str
    token_id: str
    side: str
    size: float
    usd_size: float
    price: float
    timestamp: int
    outcome: str
    market_title: str
    activity_type: str
    transaction_hash: str


class ActivityPoller:
    """Polls the Data API /activity endpoint for target traders' new trades."""

    def __init__(
        self,
        data_api: DataApiClient,
        gamma_api: GammaApiClient,
        target_traders: list[str],
        poll_interval: float = 15.0,
    ):
        self._data_api = data_api
        self._gamma_api = gamma_api
        self._targets = target_traders
        self._interval = poll_interval
        self._last_seen: dict[str, int] = {}
        self._seen_hashes: set[str] = set()
        self._trade_queue: asyncio.Queue[DetectedTrade] = asyncio.Queue()
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def trade_queue(self) -> asyncio.Queue[DetectedTrade]:
        return self._trade_queue

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Activity poller started for %d traders", len(self._targets))

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Activity poller stopped")

    def add_trader(self, address: str) -> None:
        if address not in self._targets:
            self._targets.append(address)
            logger.info("Added trader %s to polling", address)

    def remove_trader(self, address: str) -> None:
        self._targets = [t for t in self._targets if t != address]
        self._last_seen.pop(address, None)
        logger.info("Removed trader %s from polling", address)

    async def _poll_loop(self) -> None:
        stagger = self._interval / max(len(self._targets), 1)
        while self._running:
            for i, trader in enumerate(list(self._targets)):
                if not self._running:
                    break
                try:
                    await self._poll_trader(trader)
                except Exception:
                    logger.exception("Error polling trader %s", trader)
                if i < len(self._targets) - 1:
                    await asyncio.sleep(stagger)
            await asyncio.sleep(max(0, self._interval - stagger * len(self._targets)))

    async def _poll_trader(self, address: str) -> None:
        start_ts = self._last_seen.get(address)
        activities = await self._data_api.get_activity(
            user=address,
            limit=50,
            start_ts=start_ts,
            activity_type="TRADE",
        )

        if not activities:
            return

        new_count = 0
        for activity in activities:
            tx_hash = activity.get("transactionHash", activity.get("id", ""))
            if tx_hash in self._seen_hashes:
                continue

            trade = await self._parse_activity(address, activity)
            if trade:
                self._seen_hashes.add(tx_hash)
                await self._trade_queue.put(trade)
                logger.info(
                    "Detected trade: %s %s %s $%.2f @ %.4f",
                    trade.trader_address[:10],
                    trade.side,
                    trade.market_title[:30],
                    trade.usd_size,
                    trade.price,
                )
                from pm_angel.services.activity_log import activity_log
                activity_log.detect(
                    trade.trader_address, trade.market_title,
                    trade.side, trade.usd_size, trade.price,
                )
                new_count += 1

        if new_count:
            from pm_angel.services.activity_log import activity_log
            activity_log.poll(address, new_count)

        # Update last seen timestamp
        timestamps = [int(a.get("timestamp", 0)) for a in activities if a.get("timestamp")]
        if timestamps:
            self._last_seen[address] = max(timestamps)

        # Trim seen hashes to prevent unbounded growth
        if len(self._seen_hashes) > 5000:
            self._seen_hashes = set(list(self._seen_hashes)[-2000:])

    async def _parse_activity(
        self, trader_address: str, activity: dict[str, Any]
    ) -> DetectedTrade | None:
        try:
            condition_id = activity.get("conditionId", "")
            if not condition_id:
                return None

            outcome_index = int(activity.get("outcomeIndex", 0))
            side = activity.get("side", activity.get("type", "BUY")).upper()
            if side not in ("BUY", "SELL"):
                side = "BUY"

            price = float(activity.get("price", 0))
            size = float(activity.get("size", activity.get("amount", 0)))
            usd_size = float(activity.get("usdcSize", price * size if price else 0))

            # Use asset field directly if available, otherwise resolve via Gamma API
            token_id = activity.get("asset", "")
            market_title = activity.get("title", "")
            outcome = activity.get("outcome", "")

            if not token_id and condition_id:
                try:
                    market = await self._gamma_api.get_market(condition_id)
                    token_id = self._gamma_api.resolve_token_id(market, outcome_index)
                    market_title = market_title or market.get("question", market.get("title", condition_id[:16]))
                except Exception:
                    pass

            if not outcome:
                outcome = "Yes" if outcome_index == 0 else "No"
            if not market_title:
                market_title = condition_id[:16]

            return DetectedTrade(
                trader_address=trader_address,
                condition_id=condition_id,
                token_id=token_id,
                side=side,
                size=size,
                usd_size=usd_size,
                price=price,
                timestamp=int(activity.get("timestamp", int(time.time()))),
                outcome=outcome,
                market_title=market_title,
                activity_type=activity.get("type", "TRADE"),
                transaction_hash=activity.get("transactionHash", ""),
            )
        except Exception:
            logger.exception("Failed to parse activity: %s", activity)
            return None
