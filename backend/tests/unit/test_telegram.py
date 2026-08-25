import httpx
import pytest
from pydantic import SecretStr

from app.adapters import (
    DemoTelegramVendorAdapter,
    TelegramBotAdapter,
    TelegramVendorAdapter,
    parse_telegram_vendor_reply,
)
from app.config import Settings
from app.models import Vendor, WorkOrder


def test_default_config_is_telegram_sgd_250_and_exact_gemini_model() -> None:
    settings = Settings()
    assert settings.messaging_provider == "telegram"
    assert settings.currency == "SGD"
    assert settings.spending_limit_default == 250
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.google_genai_use_vertexai is False
    assert (settings.urgent_vendor_timeout_seconds, settings.routine_vendor_timeout_seconds) == (8, 12)
    assert settings.tenant_confirmation_delay_seconds == 15
    assert settings.demo_warranty_period_seconds == 30


def test_telegram_username_is_optional_but_must_not_include_at_sign() -> None:
    settings = Settings(_env_file=None, telegram_bot_username="NoMoreBucketsBot")
    assert settings.telegram_bot_username == "NoMoreBucketsBot"
    with pytest.raises(ValueError, match="without @"):
        Settings(_env_file=None, telegram_bot_username="@NoMoreBucketsBot")


def test_older_gemini_model_is_rejected() -> None:
    with pytest.raises(ValueError, match="exactly gemini-3.5-flash"):
        Settings(_env_file=None, gemini_model="gemini-1.5-flash")


def test_vendor_reply_parser_supports_buttons_followup_commands() -> None:
    assert parse_telegram_vendor_reply("ACCEPT PRICE S$180 ETA 25") == {
        "outcome": "accept",
        "amount": 180.0,
        "eta_minutes": 25,
    }
    assert parse_telegram_vendor_reply("DECLINE") == {"outcome": "decline"}
    assert parse_telegram_vendor_reply("ETA 30 minutes") == {"eta_minutes": 30}
    assert parse_telegram_vendor_reply("not a typed vendor reply") is None


def test_telegram_vendor_dispatch_sends_accept_decline_keyboard(monkeypatch) -> None:
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 77}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    settings = Settings(_env_file=None, telegram_bot_token=SecretStr("synthetic-token"))
    messaging = TelegramBotAdapter(settings)
    result = TelegramVendorAdapter(messaging).dispatch(
        WorkOrder(
            work_order_id="wo-1",
            incident_id="inc-1",
            scope="plumbing leak repair",
            currency="SGD",
            spending_limit=250,
            estimated_cost=180,
            authorized_amount=250,
        ),
        Vendor(
            vendor_id="vendor-a",
            name="Vendor A",
            region="demo",
            telegram_chat_id="2001",
        ),
        "dispatch:inc-1:vendor-a",
    )
    assert result.outcome == "pending"
    assert calls[0][1]["chat_id"] == "2001"
    keyboard = calls[0][1]["reply_markup"]["inline_keyboard"][0]
    assert [button["text"] for button in keyboard] == ["Accept", "Decline"]


def test_telegram_webhook_registration_and_status_use_api_fakes(monkeypatch) -> None:
    post_calls = []

    def fake_post(url, *, json, timeout):
        post_calls.append((url, json, timeout))
        return httpx.Response(
            200,
            json={"ok": True, "result": True},
            request=httpx.Request("POST", url),
        )

    def fake_get(url, *, timeout):
        assert url.endswith("/getWebhookInfo")
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "url": "https://demo.run.app/api/webhooks/telegram",
                    "pending_update_count": 0,
                },
            },
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "get", fake_get)
    settings = Settings(
        _env_file=None,
        telegram_bot_token=SecretStr("synthetic-token"),
        telegram_bot_username="NoMoreBucketsBot",
    )
    messaging = TelegramBotAdapter(settings)
    messaging.set_webhook("https://demo.run.app/api/webhooks/telegram", "synthetic-secret")
    info = messaging.get_webhook_info()
    assert info["pending_update_count"] == 0
    assert post_calls[0][1] == {
        "url": "https://demo.run.app/api/webhooks/telegram",
        "secret_token": "synthetic-secret",
    }


def test_live_demo_timeout_remains_pending_until_sla_task() -> None:
    settings = Settings(_env_file=None, telegram_bot_token=SecretStr("synthetic-token"))
    messaging = TelegramBotAdapter(settings)
    adapter = DemoTelegramVendorAdapter(messaging, "timeout")
    result = adapter.dispatch(
        WorkOrder(
            work_order_id="wo-2",
            incident_id="inc-2",
            scope="plumbing leak repair",
            currency="SGD",
            spending_limit=250,
            estimated_cost=180,
            authorized_amount=250,
        ),
        Vendor(
            vendor_id="vendor-a",
            name="Vendor A",
            region="demo",
            telegram_chat_id="2001",
        ),
        "dispatch:inc-2:vendor-a",
    )
    assert result.outcome == "pending"
