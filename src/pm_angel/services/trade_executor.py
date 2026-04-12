from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from pm_angel.api.clob import ClobWrapper
from pm_angel.api.gamma_api import GammaApiClient
from pm_angel.config import Settings
from pm_angel import database as db
from pm_angel.models import Bet
from pm_angel.services.activity_poller import DetectedTrade
from pm_angel.services.risk_manager import RiskManager

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Consumes detected trades from the queue and mirrors them via CLOB."""

    def __init__(
        self,
        clob: ClobWrapper,
        risk: RiskManager,
        gamma_api: GammaApiClient,
        settings: Settings,
    ):
        self._clob = clob
        self._risk = risk
        self._gamma_api = gamma_api
        self._scale = settings.position_scale_factor
        self._min_order = settings.min_order_usd
        self._max_order = settings.max_position_usd
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self, trade_queue: asyncio.Queue[DetectedTrade]) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(trade_queue))
        logger.info("Trade executor started")

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Trade executor stopped")

    async def _run(self, queue: asyncio.Queue[DetectedTrade]) -> None:
        while self._running:
            try:
                trade = await asyncio.wait_for(queue.get(), timeout=5.0)
                await self._process_trade(trade)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error processing trade")

    async def _process_trade(self, trade: DetectedTrade) -> None:
        # Calculate scaled order size
        amount = trade.usd_size * self._scale
        amount = max(self._min_order, min(amount, self._max_order))

        # Get current price for slippage check
        try:
            current_price = await self._clob.get_midpoint(trade.token_id)
        except Exception:
            current_price = trade.price

        # Risk check
        approved, reason = self._risk.check_order(
            condition_id=trade.condition_id,
            amount_usd=amount,
            current_price=current_price,
            target_price=trade.price,
        )

        from pm_angel.services.activity_log import activity_log

        if not approved:
            logger.warning(
                "Order rejected by risk manager: %s (trade: %s $%.2f)",
                reason, trade.market_title[:30], amount
            )
            activity_log.risk_rejected(trade.market_title, amount, reason)
            await self._save_bet(trade, amount, current_price, status="rejected")
            return

        activity_log.risk_approved(trade.market_title, amount)

        # Execute the order
        try:
            result = await self._clob.place_market_order(
                token_id=trade.token_id,
                amount_usd=amount,
                side=trade.side,
            )
            self._risk.record_fill(trade.condition_id, amount)
            logger.info(
                "Order filled: %s %s $%.2f -> %s",
                trade.side, trade.market_title[:30], amount, result
            )
            activity_log.order_success(trade.market_title, trade.side, amount)
            await self._save_bet(trade, amount, current_price, status="active")
        except Exception as exc:
            logger.error("Order execution failed: %s", exc)
            activity_log.order_failed(trade.market_title, str(exc))
            await self._save_bet(trade, amount, current_price, status="failed")

    async def _save_bet(
        self,
        trade: DetectedTrade,
        amount: float,
        current_price: float,
        status: str,
    ) -> None:
        if db.async_session is None:
            return

        try:
            async with db.async_session() as session:
                bet = Bet(
                    market_title=trade.market_title,
                    condition_id=trade.condition_id,
                    token_id=trade.token_id,
                    side=trade.side,
                    outcome=trade.outcome,
                    amount_usd=amount,
                    entry_price=trade.price,
                    current_price=current_price,
                    status=status,
                    pnl_absolute=0.0,
                    pnl_percent=0.0,
                    source_trader=trade.trader_address,
                    created_at=datetime.now(timezone.utc),
                )
                session.add(bet)
                await session.commit()
        except Exception:
            logger.exception("Failed to save bet to database")


async def update_prices(clob: ClobWrapper) -> None:
    """Periodic task to update current prices and PnL for active bets."""
    if db.async_session is None:
        return

    try:
        async with db.async_session() as session:
            result = await session.execute(
                select(Bet).where(Bet.status == "active")
            )
            bets = result.scalars().all()

            for bet in bets:
                try:
                    price = await clob.get_midpoint(bet.token_id)
                    bet.current_price = price
                    bet.update_pnl()
                except Exception:
                    logger.debug("Could not update price for %s", bet.token_id[:16])

            await session.commit()
    except Exception:
        logger.exception("Failed to update prices")
