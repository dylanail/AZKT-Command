from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://azkt:azkt@127.0.0.1:5432/azkt_command"
    API_HOST: str = "127.0.0.1"   # loopback only — public access via reverse proxy
    API_PORT: int = 8787

    PUBLIC_ORIGIN: str = "https://dash.arizonakeitrucks.com"
    WEBAUTHN_RP_ID: str = "dash.arizonakeitrucks.com"
    WEBAUTHN_RP_NAME: str = "AZKT Command"

    SETUP_TOKEN: str = "CHANGE_ME_ONE_TIME_SETUP_TOKEN"
    SESSION_SECRET: str = "CHANGE_ME_RANDOM_64_HEX"

    NOTION_TOKEN: str = ""
    NOTION_VEHICLES_DB_ID: str = ""
    NOTION_CUSTOMERS_DB_ID: str = ""
    NOTION_IRQS_DB_ID: str = ""
    NOTION_POLL_SECONDS: int = 45

    OPENCLAW_GATEWAY_URL: str = "http://127.0.0.1:18789"
    OPENCLAW_CLI: str = "openclaw"
    OPENCLAW_HOME: str = "~/.openclaw"

    FORBIDDEN_RECIPIENTS: str = "wordpress@azkeitrucks.com"
    DEFAULT_BUSINESS_ID: str = "AZKT"


settings = Settings()
