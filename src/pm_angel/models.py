from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Float, Integer, String, DateTime, Text, Boolean
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AppConfig(Base):
    """Key-value store for app configuration (credentials, settings)."""
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class WeatherMarket(Base):
    """A weather market discovered on Polymarket."""
    __tablename__ = "weather_markets"

    condition_id: Mapped[str] = mapped_column(String, primary_key=True)
    event_id: Mapped[str] = mapped_column(String, default="")
    title: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, default="")

    # Parsed fields
    city: Mapped[str] = mapped_column(String, default="")
    country_code: Mapped[str] = mapped_column(String, default="")
    resolves_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    market_type: Mapped[str] = mapped_column(String, default="unknown")  # rain, snow, temp_above, temp_below, unknown
    threshold_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_unit: Mapped[str] = mapped_column(String, default="")

    # CLOB tokens
    yes_token_id: Mapped[str] = mapped_column(String, default="")
    no_token_id: Mapped[str] = mapped_column(String, default="")
    yes_price: Mapped[float] = mapped_column(Float, default=0.0)
    no_price: Mapped[float] = mapped_column(Float, default=0.0)

    # Forecast
    forecast_prob_yes: Mapped[float | None] = mapped_column(Float, nullable=True)
    forecast_source: Mapped[str] = mapped_column(String, default="open-meteo")
    forecast_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    forecast_rationale: Mapped[str] = mapped_column(Text, default="")

    edge: Mapped[float | None] = mapped_column(Float, nullable=True)  # signed: positive => YES underpriced

    parse_status: Mapped[str] = mapped_column(String, default="ok")  # ok, unparseable
    parse_notes: Mapped[str] = mapped_column(Text, default="")

    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )


class WeatherDecision(Base):
    """A bot decision log entry for the Analysis page."""
    __tablename__ = "weather_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String, nullable=False)
    market_title: Mapped[str] = mapped_column(String, default="")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    market_yes_price: Mapped[float] = mapped_column(Float, default=0.0)
    market_no_price: Mapped[float] = mapped_column(Float, default=0.0)
    forecast_prob_yes: Mapped[float | None] = mapped_column(Float, nullable=True)
    edge: Mapped[float | None] = mapped_column(Float, nullable=True)
    decision: Mapped[str] = mapped_column(String, default="SKIP")  # BUY_YES, BUY_NO, SKIP
    reason: Mapped[str] = mapped_column(Text, default="")
    position_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Position(Base):
    """A bet placed by the bot."""
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    condition_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    token_id: Mapped[str] = mapped_column(String, nullable=False)
    market_title: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, default="BUY")
    outcome: Mapped[str] = mapped_column(String, default="Yes")  # Yes / No
    amount_usd: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    current_price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="pending")  # pending, active, settled, failed
    pnl_absolute: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_percent: Mapped[float] = mapped_column(Float, default=0.0)
    resolution: Mapped[str | None] = mapped_column(String, nullable=True)  # won / lost
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def update_pnl(self) -> None:
        if self.entry_price > 0 and self.status == "active":
            self.pnl_absolute = (self.current_price - self.entry_price) * (
                self.amount_usd / self.entry_price
            )
            self.pnl_percent = ((self.current_price - self.entry_price) / self.entry_price) * 100
