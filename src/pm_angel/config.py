from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    # Wallet / Auth
    private_key: str = ""
    clob_api_key: str = ""
    clob_api_secret: str = ""
    clob_api_passphrase: str = ""
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
        return bool(self.private_key and self.clob_api_key)

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> Settings:
        load_dotenv(env_path or ".env")

        traders_raw = os.getenv("TARGET_TRADERS", "")
        traders = [t.strip() for t in traders_raw.split(",") if t.strip()]

        return cls(
            private_key=os.getenv("PK", ""),
            clob_api_key=os.getenv("CLOB_API_KEY", ""),
            clob_api_secret=os.getenv("CLOB_API_SECRET", ""),
            clob_api_passphrase=os.getenv("CLOB_API_PASSPHRASE", ""),
            target_traders=traders,
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "15")),
            max_position_usd=float(os.getenv("MAX_POSITION_USD", "100")),
            max_total_exposure_usd=float(os.getenv("MAX_TOTAL_EXPOSURE_USD", "500")),
            daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT_USD", "50")),
            position_scale_factor=float(os.getenv("POSITION_SCALE_FACTOR", "0.1")),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8888")),
        )
