from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"


class Settings(BaseSettings):
    """All runtime configuration. Secrets come from the environment (Railway
    variables or a local .env); nothing here is a guessed production value."""

    model_config = SettingsConfigDict(env_file=str(REPO_ROOT / ".env"), extra="ignore")

    # ── environment ──────────────────────────────────────────────────────
    ENV: str = "development"                 # development | staging | production
    DATABASE_URL: str = "postgresql+asyncpg://azkt@127.0.0.1:5432/azkt_command"
    TEST_DATABASE_URL: str = "postgresql+asyncpg://azkt@127.0.0.1:5432/azkt_test"
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8787
    PUBLIC_ORIGIN: str = "https://dash.arizonakeitrucks.com"
    PUBLIC_API_BASE: str = ""                # empty = same origin as PUBLIC_ORIGIN
    DATA_DIR: str = str(REPO_ROOT / "data")  # local file storage (dev); S3 in prod
    DEFAULT_TIMEZONE: str = "America/Phoenix"
    DEFAULT_BUSINESS_ID: str = "AZKT"
    LOG_LEVEL: str = "info"

    # ── auth ─────────────────────────────────────────────────────────────
    WEBAUTHN_RP_ID: str = "dash.arizonakeitrucks.com"
    WEBAUTHN_RP_NAME: str = "AZKT Command"
    SETUP_TOKEN: str = "CHANGE_ME_ONE_TIME_SETUP_TOKEN"
    SESSION_SECRET: str = "CHANGE_ME_RANDOM_64_HEX"
    SESSION_MAX_AGE_SECONDS: int = 60 * 60 * 24 * 30
    ENCRYPTION_KEY: str = ""                 # Fernet key for provider tokens (required in prod)

    # ── worker / scheduling ──────────────────────────────────────────────
    WORKER_POLL_SECONDS: float = 2.0
    WORKER_BATCH: int = 10
    JOB_LEASE_SECONDS: int = 120
    REMINDER_LATE_GRACE_MINUTES: int = 10   # deliveries older than this are labelled "late"
    REMINDER_OBSOLETE_HOURS: int = 12       # older missed reminders collapse into the digest
    OVERDUE_EMAIL_DELAY_MINUTES: int = 60
    DIGEST_DEFAULT_LOCAL_TIME: str = "08:00"
    CASE_SWEEP_SECONDS: int = 300
    OUTBOX_BATCH: int = 100

    # ── model / agent runtime ────────────────────────────────────────────
    ANTHROPIC_API_KEY: str = ""              # SDK also reads this from the environment
    AZKT_MODEL: str = "claude-opus-5"
    AZKT_MODEL_LIGHT: str = "claude-sonnet-5"
    AZKT_MODEL_EFFORT: str = "high"
    MODEL_MAX_TOOL_STEPS: int = 20
    MODEL_RUN_TIMEOUT_SECONDS: int = 300
    MODEL_DAILY_BUDGET_USD: float = 0.0      # 0 = not configured -> discretionary AI work stays off
    MODEL_MONTHLY_BUDGET_USD: float = 0.0
    MAX_DELEGATION_DEPTH: int = 2
    # how many due missions one agent role may pick up in a single missions.resume_due pass, so a
    # burst of due work is drained over successive passes instead of all at once
    AGENT_ROLE_CONCURRENCY: int = 3

    # ── email (reminders) ────────────────────────────────────────────────
    EMAIL_TRANSPORT: str = "log"             # log | smtp | gmail
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_STARTTLS: bool = True
    REMINDER_FROM: str = "AZKT <reminders@azkt.app>"
    OWNER_REMINDER_EMAIL: str = ""           # verified destination; empty = email reminders held
    FORBIDDEN_RECIPIENTS: str = "wordpress@azkeitrucks.com"
    # Outside ENV=production, delivering transports (SMTP/Gmail send, website publish, Telegram) may only reach these
    # destinations: emails, "@domain" suffixes, chat ids or URL prefixes, comma-separated (H08).
    NON_PROD_DESTINATION_ALLOWLIST: str = ""

    # ── telegram ─────────────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""        # X-Telegram-Bot-Api-Secret-Token
    TELEGRAM_BOT_USERNAME: str = ""

    # ── google (gmail / drive / sheets) ──────────────────────────────────
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_PUBSUB_TOPIC: str = ""
    GOOGLE_PUBSUB_VERIFICATION_TOKEN: str = ""
    BUSINESS_EMAIL: str = "info@azkeitrucks.com"

    # ── square ───────────────────────────────────────────────────────────
    SQUARE_ACCESS_TOKEN: str = ""
    SQUARE_WEBHOOK_SIGNATURE_KEY: str = ""
    SQUARE_NOTIFICATION_URL: str = ""
    SQUARE_ENVIRONMENT: str = "sandbox"      # sandbox | production

    # ── wordpress / woocommerce ──────────────────────────────────────────
    WP_BASE_URL: str = ""
    WP_STAGING_URL: str = ""
    WP_APP_USER: str = ""
    WP_APP_PASSWORD: str = ""
    WC_CONSUMER_KEY: str = ""
    WC_CONSUMER_SECRET: str = ""

    # ── file storage ─────────────────────────────────────────────────────
    STORAGE_BACKEND: str = "local"           # local | s3
    S3_ENDPOINT: str = ""
    S3_BUCKET: str = ""
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "auto"
    UPLOAD_MAX_BYTES: int = 25 * 1024 * 1024
    UPLOAD_ALLOWED_TYPES: str = "image/jpeg,image/png,image/heic,image/heif,image/webp,audio/webm,audio/mp4,audio/mpeg,audio/ogg,audio/wav,application/pdf"

    # ── legacy (droplet era) ─────────────────────────────────────────────
    NOTION_TOKEN: str = ""
    NOTION_VEHICLES_DB_ID: str = ""
    NOTION_CUSTOMERS_DB_ID: str = ""
    NOTION_IRQS_DB_ID: str = ""
    NOTION_POLL_SECONDS: int = 45
    OPENCLAW_GATEWAY_URL: str = "http://127.0.0.1:18789"
    OPENCLAW_CLI: str = "openclaw"
    OPENCLAW_HOME: str = "~/.openclaw"
    LEGACY_BACKGROUND_LOOP: bool = False     # Notion/OpenClaw polling inside the API process

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def api_base(self) -> str:
        return self.PUBLIC_API_BASE or self.PUBLIC_ORIGIN


settings = Settings()
