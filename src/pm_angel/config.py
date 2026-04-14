from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


CONFIG_KEYS = [
    "private_key",
    "clob_api_key",
    "clob_api_secret",
    "clob_api_passphrase",
    "max_position_usd",
    "max_total_exposure_usd",
    "daily_loss_limit_usd",
    "position_scale_factor",
    "poll_interval_seconds",
]


@dataclass
class Settings:
    # Wallet / Auth
    private_key: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""
    proxy_wallet: str = ""
    chain_id: int = 137

    # Target traders
    target_traders: list[str] = field(default_factory=list)

    # Polling
    poll_interval_seconds: float = 15.0

    # Risk
    max_position_usd: float = 100.0
    max_total_exposure_usd: float = 500.0
    daily_loss_limit_usd: float = 50.0
    position_scale_factor: float = 0.1
    min_order_usd: float = 1.0
    slippage_tolerance: float = 0.05

    # Server
    host: str = "0.0.0.0"
    port: int = 8888

    # Paths
    db_path: str = "data/pm_angel.db"

    # API URLs
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    gamma_api_host: str = "https://gamma-api.polymarket.com"

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
            elif row.key == "min_order_usd":
                self.min_order_usd = float(row.value)
            elif row.key == "max_position_usd":
                self.max_position_usd = float(row.value)
            elif row.key == "max_total_exposure_usd":
                self.max_total_exposure_usd = float(row.value)
            elif row.key == "daily_loss_limit_usd":
                self.daily_loss_limit_usd = float(row.value)
            elif row.key == "position_scale_factor":
                self.position_scale_factor = float(row.value)
            elif row.key == "poll_interval_seconds":
                self.poll_interval_seconds = float(row.value)

    async def save_to_db(self, key: str, value: str) -> None:
        """Save a single config key to database."""
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

    async def save_all_to_db(self) -> None:
        """Save all config values to database."""
        pairs = {
            "private_key": self.private_key,
            "clob_api_key": self.clob_api_key,
            "clob_api_secret": self.clob_api_secret,
            "clob_api_passphrase": self.clob_api_passphrase,
            "max_position_usd": str(self.max_position_usd),
            "max_total_exposure_usd": str(self.max_total_exposure_usd),
            "daily_loss_limit_usd": str(self.daily_loss_limit_usd),
            "position_scale_factor": str(self.position_scale_factor),
            "poll_interval_seconds": str(self.poll_interval_seconds),
        }
        for k, v in pairs.items():
            if v:
                await self.save_to_db(k, v)
