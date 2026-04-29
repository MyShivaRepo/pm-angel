from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_CITIES = ["London", "New York", "Paris", "Tokyo", "Seoul"]


@dataclass
class Settings:
    # Wallet / Auth
    private_key: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""
    proxy_wallet: str = ""
    chain_id: int = 137

    # Strategy
    cities: list[str] = field(default_factory=lambda: list(DEFAULT_CITIES))
    min_edge_pct: float = 0.10
    forecast_poll_minutes: float = 60.0

    # Risk
    min_bet_usd: float = 1.0
    max_bet_usd: float = 10.0
    max_total_exposure_usd: float = 80.0

    # Server
    host: str = "0.0.0.0"
    port: int = 8888

    # Paths
    db_path: str = "data/pm_angel.db"

    # API URLs
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"
    openmeteo_host: str = "https://api.open-meteo.com"

    @property
    def has_credentials(self) -> bool:
        return bool(self.clob_api_key and self.clob_api_secret and self.clob_api_passphrase)

    @property
    def has_private_key(self) -> bool:
        return bool(self.private_key and self.private_key.startswith("0x") and len(self.private_key) == 66)

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> Settings:
        load_dotenv(env_path or ".env")
        return cls(
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8888")),
        )

    async def load_from_db(self) -> None:
        """Load credentials and settings from database."""
        from pm_angel import database as db
        from pm_angel.models import AppConfig
        from sqlalchemy import select

        if db.async_session is None:
            return

        async with db.async_session() as session:
            result = await session.execute(select(AppConfig))
            rows = result.scalars().all()

        for row in rows:
            if row.key == "private_key":
                self.private_key = row.value
            elif row.key == "clob_api_key":
                self.clob_api_key = row.value
            elif row.key == "clob_api_secret":
                self.clob_api_secret = row.value
            elif row.key == "clob_api_passphrase":
                self.clob_api_passphrase = row.value
            elif row.key == "proxy_wallet":
                self.proxy_wallet = row.value
            elif row.key == "cities":
                cities = [c.strip() for c in row.value.split(",") if c.strip()]
                if cities:
                    self.cities = cities
            elif row.key == "min_edge_pct":
                self.min_edge_pct = float(row.value)
            elif row.key == "min_bet_usd":
                self.min_bet_usd = float(row.value)
            elif row.key == "max_bet_usd":
                self.max_bet_usd = float(row.value)
            elif row.key == "max_total_exposure_usd":
                self.max_total_exposure_usd = float(row.value)
            elif row.key == "forecast_poll_minutes":
                self.forecast_poll_minutes = float(row.value)

    async def save_to_db(self, key: str, value: str) -> None:
        from pm_angel import database as db
        from pm_angel.models import AppConfig
        from sqlalchemy import select

        if db.async_session is None:
            return

        async with db.async_session() as session:
            result = await session.execute(
                select(AppConfig).where(AppConfig.key == key)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.value = value
            else:
                session.add(AppConfig(key=key, value=value))
            await session.commit()
