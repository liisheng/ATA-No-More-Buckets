from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main
from app.adapters import (
    LocalDemoVendorAdapter,
    TelegramBotAdapter,
    build_demo_media,
    build_demo_voice_media,
)
from app.config import Settings


def test_telegram_secret_start_and_duplicate_are_idempotent(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main.settings, "telegram_webhook_secret", SecretStr("demo-webhook-secret"))
    client = TestClient(main.app)
    update = {
        "update_id": 1001,
        "message": {"chat": {"id": 100000000001}, "text": "/start"},
    }
    assert (
        client.post(
            "/api/webhooks/telegram",
            json=update,
            headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
        ).status_code
        == 200
    )
    assert "100000000001" in service.started_telegram_chats
    duplicate = client.post(
        "/api/webhooks/telegram",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"


def test_telegram_bad_secret_is_rejected(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main.settings, "telegram_webhook_secret", SecretStr("demo-webhook-secret"))
    client = TestClient(main.app)
    response = client.post(
        "/api/webhooks/telegram",
        json={"update_id": 1002, "message": {"chat": {"id": 100000000001}, "text": "/start"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
    )
    assert response.status_code == 403


def test_pairing_deep_link_is_one_time_and_binds_vendor_chat(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main.settings, "demo_mode", True)
    monkeypatch.setattr(main.settings, "telegram_webhook_secret", SecretStr("demo-webhook-secret"))
    service.settings.telegram_bot_username = "NoMoreBucketsBot"
    client = TestClient(main.app)

    pairing = client.post(
        "/api/telegram/pairing-codes",
        json={"target_type": "vendor", "target_id": "vendor-b"},
    )
    assert pairing.status_code == 200
    body = pairing.json()
    assert body["deep_link"].startswith("https://t.me/NoMoreBucketsBot?start=")

    paired = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1101,
            "message": {"chat": {"id": 7001}, "text": f"/start {body['code']}"},
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert paired.status_code == 200
    assert paired.json()["kind"] == "pairing"
    assert next(v for v in service.vendors if v.vendor_id == "vendor-b").telegram_chat_id == "7001"

    reused = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1102,
            "message": {"chat": {"id": 7002}, "text": f"/start {body['code']}"},
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert reused.status_code == 403


def test_telegram_photo_voice_intake_and_callback_query(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main.settings, "telegram_webhook_secret", SecretStr("demo-webhook-secret"))
    service.vendors_adapter = LocalDemoVendorAdapter("pending")
    tenant = service.tenants["tenant-demo-001"]
    tenant.telegram_chat_id = "7003"
    service.notifications = TelegramBotAdapter(
        Settings(_env_file=None, telegram_bot_token=SecretStr("synthetic-token"))
    )
    service.notifications.send = lambda message: f"telegram:{message.action_key}"  # type: ignore[method-assign]
    service.notifications.download_media = lambda file_id, **kwargs: (  # type: ignore[method-assign]
        build_demo_voice_media(file_id)
        if kwargs.get("mime_type", "").startswith("audio/")
        else build_demo_media(file_id)
    )
    service.notifications.answer_callback = lambda callback_id: None  # type: ignore[method-assign]
    service.started_telegram_chats.add("7003")
    client = TestClient(main.app)
    start = client.post(
        "/api/webhooks/telegram",
        json={"update_id": 1200, "message": {"chat": {"id": 7003}, "text": "/start"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert start.status_code == 200
    report_start = client.post(
        "/api/webhooks/telegram",
        json={"update_id": 1201, "message": {"chat": {"id": 7003}, "text": "/report"}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert report_start.json()["kind"] == "draft_started"
    report = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1202,
            "message": {
                "chat": {"id": 7003},
                "text": "Water is dripping under the sink",
                "photo": [{"file_id": "photo-file"}],
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert report.status_code == 200
    assert report.json()["kind"] == "draft_update"
    voice = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1203,
            "message": {"chat": {"id": 7003}, "voice": {"file_id": "voice-file"}},
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert voice.json()["kind"] == "draft_update"
    draft = service.repository.list_drafts("7003")[0]
    submit = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1204,
            "callback_query": {
                "id": "draft-submit",
                "data": f"draft:{draft.draft_id}:submit",
                "message": {"chat": {"id": 7003}},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert submit.json()["kind"] == "draft_submitted"
    incident = service.list_incidents()[0]
    assert len(incident.media_ids) == 2
    assert incident.status.value == "DISPATCHING"

    callback = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1205,
            "callback_query": {
                "id": "callback-1",
                "data": f"vendor:{incident.incident_id}:accept",
                "from": {"id": "vendor-a-user"},
                "message": {"chat": {"id": "-100000000101"}},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert callback.status_code == 200
    assert callback.json()["kind"] == "vendor_callback"
    current = service.get_incident(incident.incident_id)
    assert current.assigned_vendor_id == "vendor-a"
    contacts = service.list_communications(incident.incident_id)
    assert any(
        contact.sender_role == "vendor"
        and contact.message_type == "button"
        and contact.provider_message_id == "callback-1"
        for contact in contacts
    )
    contact_count = len(contacts)
    duplicate_callback = client.post(
        "/api/webhooks/telegram",
        json={
            "update_id": 1205,
            "callback_query": {
                "id": "callback-1",
                "data": f"vendor:{incident.incident_id}:accept",
                "message": {"chat": {"id": "-100000000101"}},
            },
        },
        headers={"X-Telegram-Bot-Api-Secret-Token": "demo-webhook-secret"},
    )
    assert duplicate_callback.json()["status"] == "duplicate"
    assert len(service.list_communications(incident.incident_id)) == contact_count
