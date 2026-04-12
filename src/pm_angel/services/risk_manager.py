from __future__ import annotations

import logging
from datetime import datetime, timezone

from pm_angel.config import Settings

logger = logging.getLogger(__name__)


class RiskManager:
    """Validates orders against risk limits before execution."""

    def __init__(self, settings: Settings):
        self._max_position_usd = settings.max_position_usd
        self._max_total_exposure = settings.max_total_exposure_usd
        self._daily_loss_limit = settings.daily_loss_limit_usd
        self._min_order_usd = settings.min_order_usd
        self._slippage_tolerance = settings.slippage_tolerance

        self._positions: dict[str, float] = {}  # condition_id -> USD exposure
        self._daily_pnl: float = 0.0
        self._daily_reset: datetime = datetime.now(timezone.utc)

    @property
    def total_exposure(self) -> float:
        return sum(self._positions.values())

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def check_order(
        self,
        condition_id: str,
        amount_usd: float,
        current_price: float,
        target_price: float,
    ) -> tuple[bool, str]:
        self._maybe_reset_daily()

        if amount_usd < self._min_order_usd:
            return False, f"Order ${amount_usd:.2f} below minimum ${self._min_order_usd:.2f}"

        current_exposure = self._positions.get(condition_id, 0.0)
        if current_exposure + amount_usd > self._max_position_usd:
            return False, (
                f"Position limit: ${current_exposure + amount_usd:.2f} > "
                f"${self._max_position_usd:.2f}"
            )

        if self.total_exposure + amount_usd > self._max_total_exposure:
            return False, (
                f"Total exposure limit: ${self.total_exposure + amount_usd:.2f} > "
                f"${self._max_total_exposure:.2f}"
            )

        if self._daily_pnl < -self._daily_loss_limit:
            return False, (
                f"Daily loss limit reached: ${self._daily_pnl:.2f} < "
                f"-${self._daily_loss_limit:.2f}"
            )

        if target_price > 0 and current_price > 0:
            slippage = abs(current_price - target_price) / target_price
            if slippage > self._slippage_tolerance:
                return False, f"Slippage {slippage:.1%} exceeds {self._slippage_tolerance:.1%}"

        return True, "approved"

    def record_fill(self, condition_id: str, amount_usd: float) -> None:
        self._positions[condition_id] = self._positions.get(condition_id, 0.0) + amount_usd

    def record_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl

    def remove_position(self, condition_id: str) -> None:
        self._positions.pop(condition_id, None)

    def _maybe_reset_daily(self) -> None:
        now = datetime.now(timezone.utc)
        if now.date() > self._daily_reset.date():
            logger.info("Resetting daily PnL counter (was $%.2f)", self._daily_pnl)
            self._daily_pnl = 0.0
            self._daily_reset = now

    def get_summary(self) -> dict:
        return {
            "total_exposure": self.total_exposure,
            "daily_pnl": self._daily_pnl,
            "positions": dict(self._positions),
            "limits": {
                "max_position_usd": self._max_position_usd,
                "max_total_exposure_usd": self._max_total_exposure,
                "daily_loss_limit_usd": self._daily_loss_limit,
            },
        }
