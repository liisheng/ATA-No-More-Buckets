from __future__ import annotations

import pytest
from pydantic import SecretStr
from scripts.register_telegram_webhook import register_webhook

from app.config import Settings


class FakeTelegramAdapter:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str]] = []

    def set_webhook(self, webhook_url: str, secret_token: str) -> None:
        self.set_calls.append((webhook_url, secret_token))

    def get_webhook_info(self) -> dict[str, object]:
        return {"url": self.set_calls[-1][0], "pending_update_count": 2}


def test_registration_reads_config_and_verifies_the_webhook_without_network() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token=SecretStr("synthetic-token"),
        telegram_webhook_secret=SecretStr("synthetic-secret"),
        telegram_bot_username="NoMoreBucketsBot",
    )
    fake = FakeTelegramAdapter()
    info = register_webhook(settings, "https://demo.run.app/", fake)  # type: ignore[arg-type]
    assert info["pending_update_count"] == 2
    assert fake.set_calls == [
        ("https://demo.run.app/api/webhooks/telegram", "synthetic-secret")
    ]


def test_registration_rejects_a_mismatched_provider_url() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_token=SecretStr("synthetic-token"),
        telegram_webhook_secret=SecretStr("synthetic-secret"),
        telegram_bot_username="NoMoreBucketsBot",
    )

    class Mismatch(FakeTelegramAdapter):
        def get_webhook_info(self) -> dict[str, object]:
            return {"url": "https://other.run.app/api/webhooks/telegram"}

    with pytest.raises(RuntimeError, match="different URL"):
        register_webhook(settings, "https://demo.run.app", Mismatch())  # type: ignore[arg-type]
