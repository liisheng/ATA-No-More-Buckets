import re
from functools import lru_cache
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True
    )

    app_name: str = "No More Buckets"
    app_env: str = "demo"
    k_service: str | None = Field(default=None, validation_alias="K_SERVICE")
    demo_mode: bool = True
    adk_enabled: bool = True
    storage_backend: str = "memory"
    facts_provider: str = "deterministic"
    messaging_provider: Literal["telegram", "local", "twilio"] = "telegram"

    # Keep this exact model name visible and configurable. No fallback to an older Gemini model.
    gemini_api_key: SecretStr | None = Field(default=None, validation_alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.5-flash", validation_alias="GEMINI_MODEL")
    google_genai_use_vertexai: bool = Field(
        default=False, validation_alias="GOOGLE_GENAI_USE_VERTEXAI"
    )
    google_cloud_project: str | None = None
    google_cloud_location: str = "us-central1"
    firestore_database: str = "(default)"
    gcs_bucket: str | None = None
    pubsub_topic: str = "incident-events"
    tasks_queue: str = "incident-workflows"

    telegram_bot_token: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_webhook_secret: SecretStr | None = Field(
        default=None, validation_alias="TELEGRAM_WEBHOOK_SECRET"
    )
    telegram_bot_username: str | None = Field(
        default=None, validation_alias="TELEGRAM_BOT_USERNAME"
    )
    telegram_draft_expiry_seconds: int = Field(
        default=900, validation_alias="TELEGRAM_DRAFT_EXPIRY_SECONDS"
    )

    # Twilio is retained as an optional adapter only; Telegram is the MVP path.
    twilio_account_sid: SecretStr | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_whatsapp_from: str = "whatsapp:+14155238886"

    spending_limit_default: float = Field(default=250.0, validation_alias="SPENDING_LIMIT_DEFAULT")
    currency: str = Field(default="SGD", validation_alias="CURRENCY")
    warranty_days: int = 30
    urgent_vendor_timeout_seconds: int = Field(
        default=8, validation_alias="URGENT_VENDOR_TIMEOUT_SECONDS"
    )
    routine_vendor_timeout_seconds: int = Field(
        default=12, validation_alias="ROUTINE_VENDOR_TIMEOUT_SECONDS"
    )
    human_vendor_timeout_seconds: int = Field(
        default=600, validation_alias="HUMAN_VENDOR_TIMEOUT_SECONDS"
    )
    tenant_confirmation_delay_seconds: int = Field(
        default=15, validation_alias="TENANT_CONFIRMATION_DELAY_SECONDS"
    )
    display_timezone: str = Field(default="Asia/Singapore", validation_alias="DISPLAY_TIMEZONE")
    demo_warranty_period_seconds: int = Field(
        default=30, validation_alias="DEMO_WARRANTY_RECURRENCE_DELAY_SECONDS"
    )
    demo_vendor_a_behavior: str = "timeout"
    public_base_url: str | None = None
    cloud_tasks_invoker_service_account: str | None = None

    @field_validator("gemini_model")
    @classmethod
    def require_hackathon_model(cls, value: str) -> str:
        if value != "gemini-3.5-flash":
            raise ValueError("GEMINI_MODEL must be exactly gemini-3.5-flash")
        return value

    @field_validator("telegram_bot_username")
    @classmethod
    def validate_telegram_username(cls, value: str | None) -> str | None:
        if (
            value is not None
            and value
            and (value.startswith("@") or not re.fullmatch(r"[A-Za-z0-9_]{5,32}", value))
        ):
            raise ValueError("TELEGRAM_BOT_USERNAME must be a username without @")
        return value

    @field_validator("display_timezone")
    @classmethod
    def validate_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"DISPLAY_TIMEZONE must be a valid IANA timezone: {value}") from exc
        return value

    @field_validator("spending_limit_default", "currency")
    @classmethod
    def require_mvp_defaults(cls, value: float | str) -> float | str:
        # These values are the MVP defaults. Deployments may still override them
        # explicitly, but an empty or malformed configuration must not weaken the cap.
        if value == "" or value is None:
            raise ValueError("MVP spending/currency configuration cannot be empty")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
