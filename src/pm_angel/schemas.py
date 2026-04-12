from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class BetOut(BaseModel):
    id: int
    market_title: str
    condition_id: str
    side: str
    outcome: str
    amount_usd: float
    entry_price: float
    current_price: float
    status: str
    pnl_absolute: float
    pnl_percent: float
    source_trader: str
    created_at: datetime
    settled_at: datetime | None = None

    model_config = {"from_attributes": True}


class TraderOut(BaseModel):
    id: int
    address: str
    username: str
    pnl: float
    volume: float
    is_active: bool
    added_at: datetime

    model_config = {"from_attributes": True}


class AddTraderRequest(BaseModel):
    address: str


class SettingsUpdate(BaseModel):
    max_position_usd: float | None = None
    max_total_exposure_usd: float | None = None
    daily_loss_limit_usd: float | None = None
    position_scale_factor: float | None = None
    poll_interval_seconds: float | None = None


class BotStatus(BaseModel):
    running: bool
    balance_usdc: float | None = None
    total_positions: int = 0
    total_pnl: float = 0.0
    tracked_traders: int = 0


class LeaderboardEntry(BaseModel):
    address: str
    username: str
    pnl: float
    volume: float
    rank: int
