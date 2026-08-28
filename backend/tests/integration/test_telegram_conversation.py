from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from pydantic import SecretStr

from app import main
from app.adapters import (
    DemoClock,
    DemoTelegramVendorAdapter,
    NotificationMessage,
    build_demo_media,
    build_demo_voice_media,
)
from app.service import IncidentService


class FakeTelegram:
    provider_name = "telegram"

    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []
        self.callback_answers: list[str] = []
        self.media_sources: dict[str, str] = {}

    def send(self, message: NotificationMessage) -> str:
        self.messages.append(message)
        return f"telegram:{len(self.messages)}"

    def answer_callback(self, callback_id: str) -> None:
        self.callback_answers.append(callback_id)

    def download_media(self, file_id: str, **kwargs):
        source = str(kwargs.get("source", "tenant"))
        if file_id == "bad-after":
            source = "tenant"
        self.media_sources[file_id] = source
        if str(kwargs.get("mime_type", "")).startswith("audio/"):
            return build_demo_voice_media(file_id)
        asset = build_demo_media(file_id)
        if str(kwargs.get("mime_type", "")).startswith("video/"):
            return asset.model_copy(update={"mime_type": "video/mp4", "filename": "tenant-video.mp4"})
        return asset.model_copy(update={"source": source})


def configure(monkeypatch, service):
    fake = FakeTelegram()
    tenant = service.tenants["tenant-demo-001"]
    tenant.telegram_chat_id = "7101"
    vendor_b = next(vendor for vendor in service.vendors if vendor.vendor_id == "vendor-b")
    vendor_b.telegram_chat_id = "7202"
    service.started_telegram_chats.update({"7101", "7202"})
    service.notifications = fake
    service.vendors_adapter = DemoTelegramVendorAdapter(fake, "timeout")
    service.settings.telegram_bot_token = SecretStr("synthetic-token")
    monkeypatch.setattr(main, "service", service)
    monkeypatch.setattr(main.settings, "telegram_webhook_secret", SecretStr("conversation-secret"))
    return TestClient(main.app), fake


def post(client: TestClient, update: dict):
    return client.post(
        "/api/webhooks/telegram",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "conversation-secret"},
    )


def start_report(
    client: TestClient, update_id: int, text: str = "Water is dripping under the kitchen sink"
):
    assert (
        post(
            client, {"update_id": update_id, "message": {"chat": {"id": 7101}, "text": "/start"}}
        ).status_code
        == 200
    )
    assert (
        post(
            client,
            {"update_id": update_id + 1, "message": {"chat": {"id": 7101}, "text": "/report"}},
        ).json()["kind"]
        == "draft_started"
    )
    assert (
        post(
            client, {"update_id": update_id + 2, "message": {"chat": {"id": 7101}, "text": text}}
        ).json()["kind"]
        == "draft_update"
    )


def submit_draft(client: TestClient, service, update_id: int):
    draft = service.repository.list_drafts("7101")[0]
    response = post(
        client,
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"callback-{update_id}",
                "data": f"draft:{draft.draft_id}:submit",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert response.json()["kind"] == "draft_submitted"
    return service.list_incidents()[0]


def move_vendor_a_timeout(client: TestClient, service):
    task = next(task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout")
    attempt = next(attempt for attempt in service.get_incident(task["incident_id"]).vendor_attempts if attempt.vendor_id == "vendor-a")
    service.clock.current = attempt.deadline_at
    response = client.post(
        "/api/events/tasks",
        json={
            "task_id": task["task_id"],
            "task_type": "vendor_timeout",
            "incident_id": task["incident_id"],
            "payload": task["payload"],
        },
    )
    assert response.status_code == 200
    current = service.get_incident(task["incident_id"])
    assert current.assigned_vendor_id is None
    assert current.vendor_attempts[0].outcome == "timed_out"
    assert current.vendor_attempts[-1].vendor_id == "vendor-b"
    assert current.vendor_attempts[-1].outcome == "pending"
    return current


def accept_and_start_vendor_b(client: TestClient, incident, update_id: int):
    accept = post(
        client,
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"accept-{update_id}",
                "data": f"vendor:{incident.incident_id}:accept",
                "message": {"chat": {"id": 7202}},
            },
        },
    )
    assert accept.json()["kind"] == "vendor_callback"
    start = post(
        client,
        {
            "update_id": update_id + 1,
            "callback_query": {
                "id": f"start-{update_id}",
                "data": f"vendor:{incident.incident_id}:start",
                "message": {"chat": {"id": 7202}},
            },
        },
    )
    assert start.json()["kind"] == "vendor_callback"


def test_text_photo_voice_updates_create_exactly_one_incident_and_duplicates_are_safe(
    monkeypatch, service
):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2000)
    assert (
        post(
            client,
            {
                "update_id": 2003,
                "message": {"chat": {"id": 7101}, "photo": [{"file_id": "tenant-photo"}]},
            },
        ).json()["kind"]
        == "draft_update"
    )
    voice_update = {
        "update_id": 2004,
        "message": {"chat": {"id": 7101}, "voice": {"file_id": "tenant-voice"}},
    }
    assert post(client, voice_update).json()["kind"] == "draft_update"
    assert post(client, voice_update).json()["status"] == "duplicate"
    incident = submit_draft(client, service, 2005)
    assert len(service.list_incidents()) == 1
    assert len(incident.media_ids) == 2
    assert fake.media_sources == {"tenant-photo": "tenant", "tenant-voice": "tenant"}
    contacts = service.list_communications(incident.incident_id)
    assert sum(contact.sender_role == "tenant" for contact in contacts) >= 3
    assert (
        len({contact.provider_message_id for contact in contacts if contact.provider_message_id})
        >= 3
    )


def test_video_draft_is_visible_and_summary_updates_are_not_deduplicated(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2010)
    video = post(
        client,
        {
            "update_id": 2013,
            "message": {
                "message_id": 3013,
                "chat": {"id": 7101},
                "video": {"file_id": "tenant-video", "duration": 7, "mime_type": "video/mp4"},
                "caption": "The leak is visible from the side.",
            },
        },
    )
    assert video.json()["kind"] == "draft_update"
    draft = service.repository.list_drafts("7101")[0]
    assert sum(asset.mime_type.startswith("video/") for asset in draft.media) == 1
    assert "Videos: 1" in fake.messages[-1].text
    duplicate = post(
        client,
        {
            "update_id": 2014,
            "message": {
                "message_id": 3014,
                "chat": {"id": 7101},
                "video": {"file_id": "tenant-video", "duration": 7, "mime_type": "video/mp4"},
            },
        },
    )
    assert duplicate.json()["kind"] == "draft_duplicate_item"
    assert len(service.repository.list_drafts("7101")[0].media) == 1
    draft_response = client.get("/api/drafts")
    assert draft_response.status_code == 200
    draft_body = draft_response.json()[0]
    assert draft_body["media"][0]["mime_type"] == "video/mp4"
    assert "telegram_chat_id" not in draft_body
    media_response = client.get(draft_body["media"][0]["url"])
    assert media_response.status_code == 200


def test_telegram_album_items_and_duplicate_file_are_idempotent(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2030)
    for update_id, file_id in ((2033, "album-photo-a"), (2034, "album-photo-b")):
        response = post(
            client,
            {
                "update_id": update_id,
                "message": {
                    "message_id": update_id,
                    "media_group_id": "album-1",
                    "chat": {"id": 7101},
                    "photo": [{"file_id": file_id}],
                },
            },
        )
        assert response.json()["kind"] == "draft_update"
    duplicate = post(
        client,
        {
            "update_id": 2035,
            "message": {
                "message_id": 2035,
                "media_group_id": "album-1",
                "chat": {"id": 7101},
                "photo": [{"file_id": "album-photo-a"}],
            },
        },
    )
    assert duplicate.json()["kind"] == "draft_duplicate_item"
    draft = service.repository.list_drafts("7101")[0]
    assert [asset.asset_id for asset in draft.media] == ["album-photo-a", "album-photo-b"]


def test_delivery_readiness_survives_service_recreation(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    service.repository.seed_reference_data(service.properties, service.vendors, service.tenants)
    assert post(client, {"update_id": 2040, "message": {"chat": {"id": 7101}, "text": "/start"}}).status_code == 200
    assert post(client, {"update_id": 2041, "message": {"chat": {"id": 7202}, "text": "/start"}}).status_code == 200
    persisted = service.repository.load_reference_data()
    assert persisted is not None
    properties, vendors, tenants = persisted
    rebuilt = IncidentService(
        settings=service.settings,
        repository=service.repository,
        extractor=service.extractor,
        notifications=fake,
        vendors_adapter=service.vendors_adapter,
        evidence_verifier=service.evidence_verifier,
        media_store=service.media_store,
        event_bus=service.event_bus,
        tasks=service.tasks,
        properties=properties,
        vendors=vendors,
        tenants=tenants,
        clock=DemoClock(),
    )
    assert {"7101", "7202"}.issubset(rebuilt.started_telegram_chats)
    assert rebuilt.tenants["tenant-demo-001"].delivery_ready
    assert next(vendor for vendor in rebuilt.vendors if vendor.vendor_id == "vendor-b").delivery_ready


def test_repeated_submit_buttons_return_the_same_incident(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2050)
    draft = service.repository.list_drafts("7101")[0]
    first = post(
        client,
        {
            "update_id": 2053,
            "callback_query": {
                "id": "submit-first",
                "data": f"draft:{draft.draft_id}:submit",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    second = post(
        client,
        {
            "update_id": 2054,
            "callback_query": {
                "id": "submit-second",
                "data": f"draft:{draft.draft_id}:submit",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert first.json()["kind"] == second.json()["kind"] == "draft_submitted"
    assert len(service.list_incidents()) == 1


def test_draft_undo_clear_and_empty_submit_are_explicit(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    assert post(client, {"update_id": 2070, "message": {"chat": {"id": 7101}, "text": "/start"}}).status_code == 200
    assert post(client, {"update_id": 2071, "message": {"chat": {"id": 7101}, "text": "/report"}}).json()["kind"] == "draft_started"
    draft = service.repository.list_drafts("7101")[0]
    empty_submit = post(
        client,
        {
            "update_id": 2073,
            "callback_query": {
                "id": "empty-submit",
                "data": f"draft:{draft.draft_id}:submit",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert empty_submit.json()["kind"] == "draft_submit_rejected"
    post(client, {"update_id": 2074, "message": {"chat": {"id": 7101}, "text": "extra detail"}})
    undone = post(client, {"update_id": 2075, "message": {"chat": {"id": 7101}, "text": "/undo"}})
    assert undone.json()["kind"] == "draft_undo"
    assert not service.repository.list_drafts("7101")[0].text_parts
    cleared = post(client, {"update_id": 2076, "message": {"chat": {"id": 7101}, "text": "/clear"}})
    assert cleared.json()["kind"] == "draft_cleared"
    assert not service.repository.list_drafts("7101")[0].items
    assert not service.list_incidents()


def test_draft_cancel_and_expiry_discard_without_creating_incident(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2100)
    draft = service.repository.list_drafts("7101")[0]
    cancelled = post(
        client,
        {
            "update_id": 2103,
            "callback_query": {
                "id": "cancel-2103",
                "data": f"draft:{draft.draft_id}:cancel",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert cancelled.json()["kind"] == "draft_cancelled"
    assert not service.repository.list_drafts("7101")
    assert not service.list_incidents()

    start_report(client, 2110)
    expired = service.repository.list_drafts("7101")[0]
    service.repository.save_draft(
        expired.model_copy(update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    )
    new_report = post(
        client,
        {"update_id": 2113, "message": {"chat": {"id": 7101}, "text": "/report"}},
    )
    assert new_report.json()["kind"] == "draft_started"
    assert service.repository.list_drafts("7101")[0].draft_id != expired.draft_id


def test_vendor_timeout_dispatches_paired_vendor_b_and_late_vendor_a_cannot_win(
    monkeypatch, service
):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2200)
    incident = submit_draft(client, service, 2203)
    assert incident.status.value == "DISPATCHING"
    incident = move_vendor_a_timeout(client, service)
    assert any(message.recipient_id == "7202" and message.reply_markup for message in fake.messages)
    vendor_b_task = next(
        task
        for task in service.tasks.tasks.values()
        if task["type"] == "vendor_timeout" and task["payload"].get("vendor_id") == "vendor-b"
    )
    assert vendor_b_task["delay_seconds"] == 600
    vendor_message = next(message for message in fake.messages if message.recipient_id == "7202")
    assert "\n\nWork order:" in vendor_message.text
    assert "After acceptance, reply:\nPRICE" in vendor_message.text
    accept_and_start_vendor_b(client, incident, 2204)
    vendor_b_timeout = client.post(
        "/api/events/tasks",
        json={
            "task_id": vendor_b_task["task_id"],
            "task_type": "vendor_timeout",
            "incident_id": incident.incident_id,
            "payload": vendor_b_task["payload"],
        },
    )
    assert vendor_b_timeout.status_code == 200
    assert service.get_incident(incident.incident_id).status.value == "IN_PROGRESS"
    late = post(
        client,
        {
            "update_id": 2206,
            "callback_query": {
                "id": "late-a",
                "data": f"vendor:{incident.incident_id}:accept",
                "message": {"chat": {"id": "-100000000101"}},
            },
        },
    )
    assert late.status_code == 200
    assert service.get_incident(incident.incident_id).assigned_vendor_id == "vendor-b"
    assert any(
        entry.kind == "vendor_response_ignored"
        for entry in service.get_incident(incident.incident_id).timeline
    )


def test_over_limit_quote_escalates(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2300)
    submit_draft(client, service, 2303)
    incident = move_vendor_a_timeout(client, service)
    accept = post(
        client,
        {
            "update_id": 2304,
            "callback_query": {
                "id": "accept-2304",
                "data": f"vendor:{incident.incident_id}:accept",
                "message": {"chat": {"id": 7202}},
            },
        },
    )
    assert accept.status_code == 200
    over_limit = post(
        client,
        {"update_id": 2305, "message": {"chat": {"id": 7202}, "text": "PRICE 300 ETA 20"}},
    )
    assert over_limit.json()["kind"] == "vendor_quote+vendor_eta"
    assert service.get_incident(incident.incident_id).status.value == "ESCALATED"


def test_start_completion_evidence_and_tenant_buttons(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2400)
    incident = submit_draft(client, service, 2403)
    incident = move_vendor_a_timeout(client, service)
    accept_and_start_vendor_b(client, incident, 2404)
    completion = post(
        client,
        {
            "update_id": 2406,
            "message": {
                "chat": {"id": 7202},
                "photo": [{"file_id": "vendor-after"}],
                "caption": "COMPLETE\nPRICE 220\nSCOPE leak repair labor and replacement seal",
            },
        },
    )
    assert completion.json()["kind"] == "completion_evidence"
    current = service.get_incident(incident.incident_id)
    assert current.status.value == "PROVISIONALLY_RESOLVED"
    assert any("Still leaking" in str(message.reply_markup) for message in fake.messages)
    dry_now = post(
        client,
        {
            "update_id": 2407,
            "callback_query": {
                "id": "dry-now",
                "data": f"tenant:{incident.incident_id}:dry",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert dry_now.json()["kind"] == "tenant_confirmation"
    assert service.get_incident(incident.incident_id).status.value == "CLOSED"


def test_mismatched_completion_photo_blocks_later_completion_and_recurrence(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2500)
    incident = submit_draft(client, service, 2503)
    incident = move_vendor_a_timeout(client, service)
    accept_and_start_vendor_b(client, incident, 2504)
    service.notifications.media_sources.clear()
    bad = post(
        client,
        {
            "update_id": 2506,
            "message": {
                "chat": {"id": 7202},
                "photo": [{"file_id": "bad-after"}],
                "caption": "COMPLETE\nPRICE 220\nSCOPE leak repair labor and replacement seal",
            },
        },
    )
    assert bad.json()["kind"] == "completion_evidence"
    assert service.get_incident(incident.incident_id).status.value == "ESCALATED"
    good = post(
        client,
        {
            "update_id": 2507,
            "message": {
                "chat": {"id": 7202},
                "photo": [{"file_id": "good-after"}],
                "caption": "COMPLETE\nPRICE 220\nSCOPE leak repair labor and replacement seal",
            },
        },
    )
    assert good.json()["kind"] == "completion_evidence"
    assert service.get_incident(incident.incident_id).status.value == "ESCALATED"
    still_leaking = post(
        client,
        {
            "update_id": 2508,
            "callback_query": {
                "id": "still-leaking",
                "data": f"tenant:{incident.incident_id}:leaking",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert still_leaking.status_code == 422
    assert service.get_incident(incident.incident_id).status.value == "ESCALATED"
