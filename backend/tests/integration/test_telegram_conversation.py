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
    session = main.service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": update_id + 1, "message": {"chat": {"id": 7202}, "text": "220"}})
    post(client, {"update_id": update_id + 2, "callback_query": {"id": f"pc-{update_id}", "data": f"vs:{session.session_id}:pc", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": update_id + 3, "message": {"chat": {"id": 7202}, "text": "20"}})
    post(client, {"update_id": update_id + 4, "callback_query": {"id": f"ec-{update_id}", "data": f"vs:{session.session_id}:ec", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": update_id + 5, "callback_query": {"id": f"su-{update_id}", "data": f"vs:{session.session_id}:su", "message": {"chat": {"id": 7202}}}})
    start = post(client, {"update_id": update_id + 6, "callback_query": {"id": f"start-{update_id}", "data": f"vendor:{incident.incident_id}:start", "message": {"chat": {"id": 7202}}}})
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
    assert "Property:" in vendor_message.text
    assert "Respond within: 10 minutes" in vendor_message.text
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
    assert service.get_incident(incident.incident_id).assigned_vendor_id == "vendor-b"


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
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2305, "message": {"chat": {"id": 7202}, "text": "PRICE 300"}})
    post(client, {"update_id": 2306, "callback_query": {"id": "pc-2306", "data": f"vs:{session.session_id}:pc", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2307, "message": {"chat": {"id": 7202}, "text": "ETA 20"}})
    post(client, {"update_id": 2308, "callback_query": {"id": "ec-2308", "data": f"vs:{session.session_id}:ec", "message": {"chat": {"id": 7202}}}})
    over_limit = post(client, {"update_id": 2309, "callback_query": {"id": "su-2309", "data": f"vs:{session.session_id}:su", "message": {"chat": {"id": 7202}}}})
    assert over_limit.status_code == 200
    assert service.get_incident(incident.incident_id).status.value == "ESCALATED"


def test_start_completion_evidence_and_tenant_buttons(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2400)
    incident = submit_draft(client, service, 2403)
    incident = move_vendor_a_timeout(client, service)
    post(client, {"update_id": 2404, "callback_query": {"id": "accept-2404", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2405, "message": {"chat": {"id": 7202}, "text": "220"}})
    post(client, {"update_id": 2406, "callback_query": {"id": "pc-2406", "data": f"vs:{session.session_id}:pc", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2407, "message": {"chat": {"id": 7202}, "text": "20 minutes"}})
    post(client, {"update_id": 2408, "callback_query": {"id": "ec-2408", "data": f"vs:{session.session_id}:ec", "message": {"chat": {"id": 7202}}}})
    before_submit = service.get_incident(incident.incident_id)
    assert before_submit.eta is None and before_submit.work_order and before_submit.work_order.estimated_cost != 220
    post(client, {"update_id": 2409, "callback_query": {"id": "su-2409", "data": f"vs:{session.session_id}:su", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2410, "callback_query": {"id": "start-2410", "data": f"vendor:{incident.incident_id}:start", "message": {"chat": {"id": 7202}}}})
    photo = post(client, {"update_id": 2411, "message": {"chat": {"id": 7202}, "photo": [{"file_id": "vendor-after"}]}})
    assert photo.json()["kind"] == "completion_photo"
    post(client, {"update_id": 2412, "message": {"chat": {"id": 7202}, "text": "Replaced the failed sink seal and tested the joint."}})
    session = service.repository.get_vendor_session(session.session_id)
    assert session is not None
    assert session.stage == "CONFIRMING_FINAL_PRICE"
    assert session.final_price_confirmed is False
    post(client, {"update_id": 2413, "callback_query": {"id": "fp-2413", "data": f"vs:{session.session_id}:fp", "message": {"chat": {"id": 7202}}}})
    assert service.repository.get_vendor_session(session.session_id).stage == "COMPLETION_REVIEW"
    post(client, {"update_id": 2414, "callback_query": {"id": "cs-2414", "data": f"vs:{session.session_id}:cs", "message": {"chat": {"id": 7202}}}})
    current = service.get_incident(incident.incident_id)
    assert current.status.value == "PROVISIONALLY_RESOLVED", current.last_evidence.blocking_reasons if current.last_evidence else None
    assert any("Still leaking" in str(message.reply_markup) for message in fake.messages)
    dry_now = post(
        client,
        {
            "update_id": 2415,
            "callback_query": {
                "id": "dry-now-2415",
                "data": f"tenant:{incident.incident_id}:dry",
                "message": {"chat": {"id": 7101}},
            },
        },
    )
    assert dry_now.json()["kind"] == "tenant_confirmation"
    assert service.get_incident(incident.incident_id).status.value == "CLOSED"


def test_completion_final_price_edit_confirm_submit(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2420)
    incident = submit_draft(client, service, 2423)
    incident = move_vendor_a_timeout(client, service)
    accept_and_start_vendor_b(client, incident, 2424)
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2431, "message": {"chat": {"id": 7202}, "photo": [{"file_id": "vendor-after-edit"}]}})
    post(client, {"update_id": 2432, "message": {"chat": {"id": 7202}, "text": "Replaced the failed sink seal and tested the joint."}})
    assert service.repository.get_vendor_session(session.session_id).stage == "CONFIRMING_FINAL_PRICE"
    post(client, {"update_id": 2433, "callback_query": {"id": "cf-2433", "data": f"vs:{session.session_id}:cf", "message": {"chat": {"id": 7202}}}})
    assert service.repository.get_vendor_session(session.session_id).stage == "CONFIRMING_FINAL_PRICE"
    post(client, {"update_id": 2434, "message": {"chat": {"id": 7202}, "text": "225"}})
    assert service.repository.get_vendor_session(session.session_id).final_price == 225
    post(client, {"update_id": 2435, "callback_query": {"id": "fp-2435", "data": f"vs:{session.session_id}:fp", "message": {"chat": {"id": 7202}}}})
    review = service.repository.get_vendor_session(session.session_id)
    assert review.stage == "COMPLETION_REVIEW" and review.final_price_confirmed is True
    assert any("Submit completion" in str(message.reply_markup) for message in service.notifications.messages)
    post(client, {"update_id": 2436, "callback_query": {"id": "cs-2436", "data": f"vs:{session.session_id}:cs", "message": {"chat": {"id": 7202}}}})
    assert service.get_incident(incident.incident_id).status.value == "PROVISIONALLY_RESOLVED"


def test_cancel_completion_draft_keeps_accepted_job_resumable(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2440)
    incident = submit_draft(client, service, 2443)
    incident = move_vendor_a_timeout(client, service)
    accept_and_start_vendor_b(client, incident, 2444)
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2451, "message": {"chat": {"id": 7202}, "photo": [{"file_id": "vendor-after-cancel"}]}})
    cancelled = post(client, {"update_id": 2452, "message": {"chat": {"id": 7202}, "text": "/cancel"}})
    assert cancelled.json()["kind"] == "vendor_cancelled"
    recovered = service.repository.get_vendor_session(session.session_id)
    assert recovered.stage == "SUBMITTED" and recovered.cancelled is False
    assert recovered.completion_photo_ids == [] and recovered.completion_scope is None
    assert service.get_incident(incident.incident_id).status.value == "IN_PROGRESS"
    resumed_start = post(client, {"update_id": 2453, "message": {"chat": {"id": 7202}, "text": "/complete"}})
    assert resumed_start.json()["kind"] == "vendor_completion_started"
    resumed = post(client, {"update_id": 2454, "message": {"chat": {"id": 7202}, "photo": [{"file_id": "vendor-after-resumed"}]}})
    assert resumed.json()["kind"] == "completion_photo"
    cancelled_again = post(client, {"update_id": 2455, "callback_query": {"id": "cx-2455", "data": f"vs:{session.session_id}:cx", "message": {"chat": {"id": 7202}}}})
    assert cancelled_again.json()["kind"] == "vendor_session_callback"
    assert service.repository.get_vendor_session(session.session_id).stage == "SUBMITTED"


def test_cancel_accepted_intake_resets_before_quote_submission(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2460)
    incident = submit_draft(client, service, 2463)
    incident = move_vendor_a_timeout(client, service)
    post(client, {"update_id": 2464, "callback_query": {"id": "accept-2464", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    session = service.repository.list_vendor_sessions("7202")[0]
    assert session.stage == "AWAITING_PRICE"
    cancelled = post(client, {"update_id": 2465, "message": {"chat": {"id": 7202}, "text": "/cancel"}})
    assert cancelled.json()["kind"] == "vendor_cancelled"
    recovered = service.repository.get_vendor_session(session.session_id)
    assert recovered.stage == "AWAITING_PRICE" and recovered.cancelled is False
    assert recovered.draft_price is None and recovered.draft_eta is None
    current = service.get_incident(incident.incident_id)
    assert current.status.value == "SCHEDULED" and current.assigned_vendor_id == "vendor-b"
    resumed = post(client, {"update_id": 2466, "message": {"chat": {"id": 7202}, "text": "220"}})
    assert resumed.json()["kind"] == "vendor_price"


def test_premature_complete_returns_current_vendor_step_help(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2470)
    incident = submit_draft(client, service, 2473)
    incident = move_vendor_a_timeout(client, service)
    post(client, {"update_id": 2474, "callback_query": {"id": "accept-2474", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    response = post(client, {"update_id": 2475, "message": {"chat": {"id": 7202}, "text": "/complete"}})
    assert response.status_code == 200
    assert response.json()["kind"] == "vendor_step_help"
    assert "Current step: AWAITING_PRICE" in fake.messages[-1].text


def test_cancel_after_quote_submission_returns_current_vendor_step_help(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2480)
    incident = submit_draft(client, service, 2483)
    incident = move_vendor_a_timeout(client, service)
    post(client, {"update_id": 2484, "callback_query": {"id": "accept-2484", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2485, "message": {"chat": {"id": 7202}, "text": "220"}})
    post(client, {"update_id": 2486, "callback_query": {"id": "pc-2486", "data": f"vs:{session.session_id}:pc", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2487, "message": {"chat": {"id": 7202}, "text": "20"}})
    post(client, {"update_id": 2488, "callback_query": {"id": "ec-2488", "data": f"vs:{session.session_id}:ec", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2489, "callback_query": {"id": "su-2489", "data": f"vs:{session.session_id}:su", "message": {"chat": {"id": 7202}}}})
    assert service.repository.get_vendor_session(session.session_id).stage == "SUBMITTED"

    response = post(client, {"update_id": 2490, "message": {"chat": {"id": 7202}, "text": "/cancel"}})
    assert response.status_code == 200
    assert response.json()["kind"] == "vendor_step_help"
    assert "Current step: SUBMITTED" in fake.messages[-1].text
    assert service.repository.get_vendor_session(session.session_id).stage == "SUBMITTED"


def test_cancel_offered_vendor_session_is_rejected_and_accept_remains_available(monkeypatch, service):
    client, fake = configure(monkeypatch, service)
    start_report(client, 2495)
    incident = submit_draft(client, service, 2498)
    incident = move_vendor_a_timeout(client, service)
    session = service.repository.list_vendor_sessions("7202")[0]
    assert session.stage == "OFFERED"

    cancelled = post(client, {"update_id": 2499, "message": {"chat": {"id": 7202}, "text": "/cancel"}})
    assert cancelled.status_code == 200
    assert cancelled.json()["kind"] == "vendor_step_help"
    assert "Current step: OFFERED" in fake.messages[-1].text
    assert service.repository.get_vendor_session(session.session_id).stage == "OFFERED"
    assert service.repository.get_vendor_session(session.session_id).cancelled is False

    accepted = post(client, {"update_id": 2500, "callback_query": {"id": "accept-2500", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    assert accepted.status_code == 200
    assert accepted.json()["kind"] == "vendor_callback"
    assert service.repository.get_vendor_session(session.session_id).stage == "AWAITING_PRICE"
    assert service.get_incident(incident.incident_id).assigned_vendor_id == "vendor-b"


def test_invalid_price_eta_and_photo_keep_the_current_step(monkeypatch, service):
    client, _ = configure(monkeypatch, service)
    start_report(client, 2500)
    incident = submit_draft(client, service, 2503)
    incident = move_vendor_a_timeout(client, service)
    post(client, {"update_id": 2504, "callback_query": {"id": "accept-2504", "data": f"vendor:{incident.incident_id}:accept", "message": {"chat": {"id": 7202}}}})
    session = service.repository.list_vendor_sessions("7202")[0]
    post(client, {"update_id": 2505, "message": {"chat": {"id": 7202}, "text": "not a price"}})
    assert service.repository.get_vendor_session(session.session_id).stage == "AWAITING_PRICE"
    post(client, {"update_id": 2506, "message": {"chat": {"id": 7202}, "text": "220"}})
    post(client, {"update_id": 2507, "callback_query": {"id": "pc-2507", "data": f"vs:{session.session_id}:pc", "message": {"chat": {"id": 7202}}}})
    post(client, {"update_id": 2508, "message": {"chat": {"id": 7202}, "text": "12.5"}})
    assert service.repository.get_vendor_session(session.session_id).stage == "AWAITING_ETA"
