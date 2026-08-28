from __future__ import annotations

import base64
import hmac
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .adapters import (
    CompletionEvidenceVerifier,
    DemoClock,
    DemoTelegramVendorAdapter,
    DeterministicCompletionEvidenceVerifier,
    DeterministicFactExtractor,
    EventBus,
    FactExtractor,
    GeminiCompletionEvidenceVerifier,
    LocalDemoNotificationAdapter,
    LocalDemoVendorAdapter,
    LocalEventBus,
    LocalMediaStore,
    LocalTaskQueue,
    MediaStore,
    MessagingPort,
    NotificationMessage,
    SystemClock,
    TaskQueue,
    TelegramBotAdapter,
    TelegramVendorAdapter,
    TwilioWhatsAppAdapter,
    VendorAdapter,
    VertexGeminiFactExtractor,
    build_demo_media,
    build_demo_voice_media,
    parse_telegram_eta,
    parse_telegram_legacy_vendor_input,
    parse_telegram_price,
)
from .catalog import demo_properties, demo_tenants, demo_vendors
from .cloud_adapters import (
    GoogleCloudStorageMediaStore,
    GoogleCloudTasksQueue,
    GooglePubSubEventBus,
)
from .config import Settings, get_settings
from .logging_config import configure_logging
from .models import (
    ActionRequest,
    CommunicationRecord,
    Incident,
    MediaAsset,
    MediaDescriptor,
    PairingCodeRequest,
    PairingCodeResponse,
    ReportInput,
    TaskEvent,
    TelegramDraft,
    TenantContact,
    Vendor,
    VendorSession,
)
from .repositories import FirestoreIncidentRepository, InMemoryIncidentRepository
from .service import IncidentNotFound, IncidentService


def _demo_properties() -> dict:
    return demo_properties()


def _demo_vendors() -> list:
    return demo_vendors()


def _demo_tenants() -> dict[str, TenantContact]:
    return demo_tenants()


def create_service(settings: Settings | None = None) -> IncidentService:
    settings = settings or get_settings()
    agent = None
    if settings.adk_enabled and settings.facts_provider.lower() in {"gemini", "vertex"}:
        from .adk_agent import build_adk_agent

        agent = build_adk_agent(settings)
    cloud = settings.storage_backend.lower() in {"gcp", "firestore", "cloud"}
    repository = (
        FirestoreIncidentRepository(settings.google_cloud_project, settings.firestore_database)
        if cloud
        else InMemoryIncidentRepository()
    )
    if settings.facts_provider.lower() in {"gemini", "vertex"}:
        extractor: FactExtractor = VertexGeminiFactExtractor(settings)
    else:
        extractor = DeterministicFactExtractor()
    notifications: MessagingPort
    vendors_adapter: VendorAdapter
    evidence_verifier: CompletionEvidenceVerifier
    if (
        settings.demo_mode
        and settings.messaging_provider.lower() == "telegram"
        and settings.telegram_bot_token
    ):
        notifications = TelegramBotAdapter(settings)
        vendors_adapter = DemoTelegramVendorAdapter(notifications, settings.demo_vendor_a_behavior)
        evidence_verifier = DeterministicCompletionEvidenceVerifier()
    elif settings.demo_mode:
        notifications = LocalDemoNotificationAdapter()
        vendors_adapter = LocalDemoVendorAdapter(settings.demo_vendor_a_behavior)
        evidence_verifier = DeterministicCompletionEvidenceVerifier()
    elif settings.messaging_provider.lower() == "telegram":
        notifications = TelegramBotAdapter(settings)
        vendors_adapter = TelegramVendorAdapter(notifications)
        evidence_verifier = GeminiCompletionEvidenceVerifier(settings)
    elif settings.messaging_provider.lower() == "twilio":
        notifications = TwilioWhatsAppAdapter(settings)
        vendors_adapter = LocalDemoVendorAdapter(settings.demo_vendor_a_behavior)
        evidence_verifier = GeminiCompletionEvidenceVerifier(settings)
    elif settings.messaging_provider.lower() == "local":
        notifications = LocalDemoNotificationAdapter()
        vendors_adapter = LocalDemoVendorAdapter(settings.demo_vendor_a_behavior)
        evidence_verifier = DeterministicCompletionEvidenceVerifier()
    else:
        raise ValueError("MESSAGING_PROVIDER must be telegram, local, or optional twilio")
    if cloud:
        media_store: MediaStore = GoogleCloudStorageMediaStore(settings)
        event_bus: EventBus = GooglePubSubEventBus(settings)
        tasks: TaskQueue = GoogleCloudTasksQueue(settings)
    else:
        media_store = LocalMediaStore()
        event_bus = LocalEventBus()
        tasks = LocalTaskQueue()
    properties = _demo_properties()
    vendors = _demo_vendors()
    tenants = _demo_tenants()
    repository.seed_reference_data(properties, vendors, tenants)
    persisted_reference = repository.load_reference_data()
    if persisted_reference:
        properties, vendors, tenants = persisted_reference
    service = IncidentService(
        settings=settings,
        repository=repository,
        extractor=extractor,
        notifications=notifications,
        vendors_adapter=vendors_adapter,
        evidence_verifier=evidence_verifier,
        media_store=media_store,
        event_bus=event_bus,
        tasks=tasks,
        properties=properties,
        vendors=vendors,
        tenants=tenants,
        clock=DemoClock() if settings.demo_mode and not cloud and not settings.k_service else SystemClock(),
        agent=agent,
    )
    service.resume_pending_workflows()
    return service


settings = get_settings()
configure_logging(settings)
service = create_service(settings)
app = FastAPI(title=settings.app_name, version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_demo_api() -> None:
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail="demo API is disabled")


def _require_local_replay_api() -> None:
    _require_demo_api()
    if service.live_cloud or service.notifications.provider_name == "telegram":
        raise HTTPException(status_code=404, detail="deterministic replay is disabled in live mode")


def _require_local_draft_api() -> None:
    _require_demo_api()
    if service.live_cloud:
        raise HTTPException(status_code=404, detail="draft inspection is disabled in live mode")


def _tenant_for_chat(chat_id: str) -> TenantContact | None:
    return next(
        (tenant for tenant in service.tenants.values() if tenant.telegram_chat_id == chat_id), None
    )


def _vendor_for_chat(chat_id: str) -> Vendor | None:
    return next((vendor for vendor in service.vendors if vendor.telegram_chat_id == chat_id), None)


def _telegram_sender_id(payload: dict) -> str | None:
    sender = payload.get("from")
    if not isinstance(sender, dict) or sender.get("id") is None:
        return None
    return str(sender["id"])


def _authorized_vendor_sender(vendor: Vendor, sender_id: str | None) -> bool:
    return bool(sender_id and sender_id in vendor.authorized_telegram_user_ids)


def _vendor_session_for_chat(chat_id: str, session_id: str | None = None, incident_id: str | None = None, vendor_id: str | None = None) -> VendorSession | None:
    if incident_id and vendor_id:
        return service.repository.find_vendor_session(chat_id, vendor_id, incident_id)
    terminal_incidents = {"ESCALATED", "CLOSED", "CANCELLED", "PROVISIONALLY_RESOLVED"}
    sessions = [s for s in service.repository.list_vendor_sessions(chat_id)
                if not s.cancelled and s.stage not in {"CANCELLED", "DECLINED", "TIMED_OUT", "RELEASED", "COMPLETED"}
                and (incident := service.repository.get(s.incident_id)) is not None
                and incident.status.value not in terminal_incidents]
    if session_id:
        return next((s for s in sessions if s.session_id == session_id), None)
    active = [s for s in sessions if s.stage != "SUBMITTED" or service.get_incident(s.incident_id).status.value in {"SCHEDULED", "IN_PROGRESS"}]
    if len(active) == 1:
        return active[0]
    return sessions[0] if len(sessions) == 1 else None


def _vendor_help(session: VendorSession | None) -> str:
    if not session:
        return "No single active vendor intake is available. Use /status after opening the current work-order message."
    next_action = {
        "OFFERED": "Tap Accept job or Decline job.",
        "AWAITING_PRICE": "Send one SGD amount, for example 220 or PRICE 220.50.",
        "CONFIRMING_PRICE": "Tap the quote confirmation or Edit price.",
        "AWAITING_ETA": "Send whole minutes, for example 20 or ETA 20.",
        "CONFIRMING_ETA": "Tap the ETA confirmation, Edit ETA, or Back to price.",
        "REVIEW": "Review the quote and ETA, then tap Submit quote and ETA.",
        "SUBMITTED": "Tap Start job when you arrive. When the repair is finished, send /complete.",
        "AWAITING_FINAL_APPROVAL": "The final price is awaiting manager approval; your completion draft is saved.",
        "AWAITING_PHOTO": "Attach one clear after-photo.",
        "AWAITING_SCOPE": "Send a 10–500 character work summary.",
        "CONFIRMING_FINAL_PRICE": "Confirm the final price, or tap Change final price and send a new amount.",
        "COMPLETION_REVIEW": "Review the completion and tap Submit completion.",
    }.get(session.stage, "Use /status for the current step.")
    values = f"Quote: S${session.draft_price:.2f}" if session.draft_price is not None else "Quote: not captured"
    values += f"\nArrival ETA: {session.draft_eta} minutes" if session.draft_eta is not None else "\nArrival ETA: not captured"
    return f"Current step: {session.stage}\n{values}\nNext: {next_action}"


def _active_vendor_incident(vendor_id: str) -> Incident | None:
    active_statuses = {
        "DISPATCHING",
        "SCHEDULED",
        "IN_PROGRESS",
        "VERIFYING",
        "ESCALATED",
    }
    matches = [
        incident
        for incident in service.list_incidents()
        if incident.status.value in active_statuses
        and (
            incident.assigned_vendor_id == vendor_id
            or any(attempt.vendor_id == vendor_id for attempt in incident.vendor_attempts)
        )
    ]
    return max(matches, key=lambda incident: incident.updated_at) if matches else None


def _active_tenant_confirmation(tenant_id: str, incident_id: str | None = None) -> Incident | None:
    matches = [
        incident
        for incident in service.list_incidents()
        if incident.tenant_id == tenant_id
        and (incident_id is None or incident.incident_id == incident_id)
        and incident.status.value in {"PROVISIONALLY_RESOLVED", "CLOSED"}
    ]
    return max(matches, key=lambda incident: incident.updated_at) if matches else None


def _telegram_media(message: dict, source: str = "tenant") -> list:
    if not hasattr(service.notifications, "download_media"):
        raise HTTPException(status_code=503, detail="Telegram adapter is not active")
    media = []
    photos = message.get("photo", [])
    if isinstance(photos, list) and photos:
        photo = photos[-1]
        if isinstance(photo, dict) and isinstance(photo.get("file_id"), str):
            media.append(
                service.notifications.download_media(
                    photo["file_id"],
                    mime_type="image/jpeg",
                    filename="telegram-photo.jpg",
                    source=source,
                )
            )
    voice = message.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        media.append(
            service.notifications.download_media(
                voice["file_id"],
                mime_type="audio/ogg",
                filename="telegram-voice.ogg",
                source=source,
                duration_seconds=(
                    int(voice["duration"]) if isinstance(voice.get("duration"), int) else None
                ),
            )
        )
    video = message.get("video")
    if isinstance(video, dict) and isinstance(video.get("file_id"), str):
        media.append(
            service.notifications.download_media(
                video["file_id"],
                mime_type=str(video.get("mime_type") or "video/mp4"),
                filename="telegram-video.mp4",
                source=source,
                duration_seconds=(
                    int(video["duration"]) if isinstance(video.get("duration"), int) else None
                ),
            )
        )
    video_note = message.get("video_note")
    if isinstance(video_note, dict) and isinstance(video_note.get("file_id"), str):
        media.append(
            service.notifications.download_media(
                video_note["file_id"],
                mime_type="video/mp4",
                filename="telegram-video-note.mp4",
                source=source,
                duration_seconds=(
                    int(video_note["duration"])
                    if isinstance(video_note.get("duration"), int)
                    else None
                ),
            )
        )
    return media


def _draft_markup(draft: TelegramDraft) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Submit report", "callback_data": f"draft:{draft.draft_id}:submit"},
                {"text": "➕ Add / edit items", "callback_data": f"draft:{draft.draft_id}:add"},
            ],
            [
                {"text": "↩️ Undo last", "callback_data": f"draft:{draft.draft_id}:undo"},
                {"text": "🗑 Cancel", "callback_data": f"draft:{draft.draft_id}:cancel"},
            ],
        ]
    }


def _draft_item_key(update_id: int, message: dict) -> str:
    """Build a stable key from Telegram's delivery identifiers."""

    message_id = message.get("message_id")
    media_group_id = message.get("media_group_id")
    file_ids: list[str] = []
    for field in ("photo", "video", "voice", "video_note"):
        value = message.get(field)
        if field == "photo" and isinstance(value, list) and value:
            candidate = value[-1]
        else:
            candidate = value
        if isinstance(candidate, dict) and isinstance(candidate.get("file_id"), str):
            file_ids.append(candidate["file_id"])
    if file_ids:
        prefix = f"album:{media_group_id}" if media_group_id else "media"
        return f"{prefix}:{','.join(file_ids)}"
    return f"message:{message.get('chat', {}).get('id', '')}:{message_id or update_id}"


def _answer_callback(callback_id: object, text: str | None = None, show_alert: bool = False) -> None:
    if not isinstance(callback_id, str) or not hasattr(service.notifications, "answer_callback"):
        return
    try:
        service.notifications.answer_callback(callback_id, text=text, show_alert=show_alert)
    except TypeError:
        # Keep simple fake adapters and older local adapters compatible.
        service.notifications.answer_callback(callback_id)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.post(
    "/api/telegram/pairing-codes",
    response_model=PairingCodeResponse,
    include_in_schema=True,
)
@app.post("/api/pairing/codes", response_model=PairingCodeResponse, include_in_schema=False)
def create_telegram_pairing_code(request: PairingCodeRequest) -> PairingCodeResponse:
    _require_demo_api()
    try:
        return service.create_pairing_code(request.target_type, request.target_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/runtime")
def runtime() -> dict:
    return service.runtime_metadata().model_dump(mode="json")


@app.get("/api/incidents")
def list_incidents() -> list[Incident]:
    return service.list_incidents()


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str) -> Incident:
    try:
        return service.get_incident(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc


@app.get("/api/incidents/{incident_id}/communications")
def list_communications(incident_id: str) -> list[CommunicationRecord]:
    try:
        return service.list_communications(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc


@app.get("/api/incidents/{incident_id}/media")
def list_media(incident_id: str) -> list[MediaDescriptor]:
    try:
        incident = service.get_incident(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc
    descriptors: list[MediaDescriptor] = []
    for media_id in incident.media_ids:
        asset = service.media_store.get(media_id)
        if asset:
            descriptors.append(
                MediaDescriptor(
                    media_id=asset.asset_id,
                    filename=asset.filename.replace("\\", "/").rsplit("/", 1)[-1],
                    mime_type=asset.mime_type,
                    size_bytes=asset.size_bytes,
                    duration_seconds=asset.duration_seconds,
                    source=asset.source,
                    url=f"/api/incidents/{incident_id}/media/{asset.asset_id}",
                )
            )
    return descriptors


@app.get("/api/drafts")
def list_drafts() -> list[dict]:
    _require_local_draft_api()
    result: list[dict] = []
    for draft in service.list_active_telegram_drafts():
        descriptors: list[dict] = []
        for asset in draft.media:
            descriptors.append(
                MediaDescriptor(
                    media_id=asset.asset_id,
                    filename=asset.filename.replace("\\", "/").rsplit("/", 1)[-1],
                    mime_type=asset.mime_type,
                    size_bytes=asset.size_bytes,
                    duration_seconds=asset.duration_seconds,
                    source=asset.source,
                    url=f"/api/drafts/{draft.draft_id}/media/{asset.asset_id}",
                ).model_dump(mode="json")
            )
        result.append(
            {
                "draft_id": draft.draft_id,
                "tenant_id": draft.tenant_id,
                "property_id": draft.property_id,
                "text_parts": draft.text_parts,
                "media": descriptors,
                "communications": [
                    record.model_dump(mode="json")
                    for record in service.list_draft_communications(draft.draft_id)
                ],
                "created_at": draft.created_at,
                "updated_at": draft.updated_at,
                "expires_at": draft.expires_at,
            }
        )
    return result


@app.get("/api/incidents/{incident_id}/media/{media_id}")
def get_media(incident_id: str, media_id: str, request: Request) -> Response:
    try:
        incident = service.get_incident(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc
    if media_id not in incident.media_ids:
        raise HTTPException(status_code=404, detail="media not found for incident")
    asset = service.media_store.get(media_id)
    if not asset or not asset.content_base64:
        raise HTTPException(status_code=404, detail="media is unavailable")
    try:
        content = base64.b64decode(asset.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="stored media is invalid") from exc
    filename = asset.filename.replace("\\", "/").rsplit("/", 1)[-1] or "media.bin"
    return _media_response(asset, content, filename, request)


def _media_response(asset: MediaAsset, content: bytes, filename: str, request: Request) -> Response:
    headers = {
        "Content-Disposition": f'inline; filename="{filename.replace(chr(34), "")}"',
        "Cache-Control": "private, max-age=60",
        "Accept-Ranges": "bytes",
    }
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes=") and asset.mime_type.startswith("video/"):
        start_end = range_header[6:].split("-", 1)
        try:
            start = int(start_end[0] or 0)
            end = int(start_end[1]) if len(start_end) > 1 and start_end[1] else len(content) - 1
        except ValueError:
            start, end = 0, len(content) - 1
        if 0 <= start <= end < len(content):
            body = content[start : end + 1]
            headers.update(
                {
                    "Content-Range": f"bytes {start}-{end}/{len(content)}",
                    "Content-Length": str(len(body)),
                }
            )
            return Response(content=body, status_code=206, media_type=asset.mime_type, headers=headers)
    return Response(content=content, media_type=asset.mime_type, headers=headers)


@app.get("/api/drafts/{draft_id}/media/{media_id}")
def get_draft_media(draft_id: str, media_id: str, request: Request) -> Response:
    _require_local_draft_api()
    draft = next(
        (candidate for candidate in service.list_active_telegram_drafts() if candidate.draft_id == draft_id),
        None,
    )
    if not draft or media_id not in {asset.asset_id for asset in draft.media}:
        raise HTTPException(status_code=404, detail="media not found for draft")
    asset = service.media_store.get(media_id)
    if not asset or not asset.content_base64:
        raise HTTPException(status_code=404, detail="media is unavailable")
    try:
        content = base64.b64decode(asset.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="stored media is invalid") from exc
    filename = asset.filename.replace("\\", "/").rsplit("/", 1)[-1] or "media.bin"
    return _media_response(asset, content, filename, request)


@app.post("/api/incidents", response_model=Incident)
def create_incident(report: ReportInput) -> Incident:
    _require_demo_api()
    try:
        return service.submit_report(report)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/incidents/{incident_id}/actions", response_model=Incident)
def incident_action(incident_id: str, request: ActionRequest) -> Incident:
    _require_demo_api()
    try:
        return service.process_action(incident_id, request)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/demo/seed", response_model=Incident)
def seed_demo() -> Incident:
    _require_local_replay_api()
    media = build_demo_media()
    report = ReportInput(
        property_id="demo-tampines-101",
        tenant_id="tenant-demo-001",
        report_text="There is water dripping under the kitchen sink and the cabinet is wet. Photo attached.",
        voice_transcript="It is a steady leak but the electricity is dry and I can reach the shutoff.",
        media=[media, build_demo_voice_media()],
        idempotency_key=f"demo-seed:{uuid4().hex}",
    )
    return service.submit_report(report)


@app.post("/api/demo/reset")
def reset_demo() -> JSONResponse:
    _require_local_replay_api()
    global service
    service.shutdown()
    service = create_service(settings)
    return JSONResponse({"status": "reset"})


@app.post("/api/events/tasks")
def task_event(payload: TaskEvent) -> dict[str, str]:
    # The stable task ID becomes the action event ID; repeated Cloud Tasks delivery is a no-op.
    task_id = payload.task_id
    task_type = payload.task_type
    incident_id = payload.incident_id
    if incident_id and task_id != "unknown":
        service.record_communication(
            communication_id=f"comm:scheduler:{task_id}",
            incident_id=incident_id,
            sender_role="scheduler",
            sender_id=service.tasks.provider_name,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel=service.tasks.provider_name,
            direction="inbound",
            message_type="system",
            text=f"Scheduler delivered {task_type}.",
            provider_message_id=task_id,
            delivery_status=(
                "received" if service.tasks.provider_name == "cloud_tasks" else "simulated"
            ),
        )
    try:
        if task_type == "tenant_confirmation":
            service.request_tenant_confirmation(incident_id, event_id=f"task-{task_id}")
        elif task_type == "vendor_timeout":
            service.process_action(
                incident_id,
                ActionRequest(
                    action="vendor_timeout",
                    event_id=f"task-{task_id}",
                    payload={"vendor_id": payload.payload.get("vendor_id", "")},
                ),
            )
        elif task_type == "vendor_retry":
            service.process_action(
                incident_id,
                ActionRequest(
                    action="vendor_retry",
                    event_id=f"task-{task_id}",
                    payload={"vendor_id": payload.payload.get("vendor_id", "")},
                ),
            )
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc
    return {"status": "accepted", "task_id": task_id}


@app.post("/api/events/pubsub")
def pubsub_event(payload: dict) -> dict[str, str]:
    """Accept standard Pub/Sub push envelopes and claim delivery exactly once."""
    message = payload.get("message", {})
    if not isinstance(message, dict):
        raise HTTPException(status_code=422, detail="invalid Pub/Sub push envelope")
    message_id = str(message.get("messageId", ""))
    data = message.get("data")
    envelope: dict = {}
    if isinstance(data, str):
        try:
            decoded = base64.b64decode(data, validate=True).decode("utf-8")
            candidate = json.loads(decoded)
            if isinstance(candidate, dict):
                envelope = candidate
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="invalid Pub/Sub message data") from None
    event_id = str(envelope.get("event_id") or message_id)
    if not event_id:
        raise HTTPException(status_code=422, detail="Pub/Sub message ID is required")
    claimed = service.repository.claim_event(f"pubsub:{event_id}")
    return {
        "status": "acknowledged" if claimed else "duplicate",
        "message_id": message_id or event_id,
    }


@app.post("/api/webhooks/telegram")
def telegram_webhook(
    update: dict,
    telegram_secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
) -> dict[str, str]:
    expected = (
        settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret
        else None
    )
    if not expected or not telegram_secret or not hmac.compare_digest(expected, telegram_secret):
        raise HTTPException(status_code=403, detail="invalid Telegram webhook secret")
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        raise HTTPException(status_code=422, detail="Telegram update_id is required")
    if not service.repository.claim_event(f"telegram-webhook-{update_id}"):
        return {"status": "duplicate", "kind": "telegram_update"}

    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", "")) if isinstance(message, dict) else ""
        callback_data = callback.get("data", "")
        parts = callback_data.split(":") if isinstance(callback_data, str) else []
        provider_message_id = str(callback.get("id") or message.get("message_id") or update_id)
        callback_vendor = _vendor_for_chat(chat_id)
        if callback_vendor and not _authorized_vendor_sender(callback_vendor, _telegram_sender_id(callback)):
            _answer_callback(callback.get("id"), "This Telegram user is not authorized for this vendor job.", True)
            return {"status": "processed", "kind": "vendor_sender_unauthorized"}

        if len(parts) == 3 and parts[0] == "draft":
            if parts[2] == "add":
                _answer_callback(
                    callback.get("id"),
                    "Use the message box, 📎 attachment button, or 🎙 microphone below.",
                )
            else:
                _answer_callback(callback.get("id"))
            tenant = _tenant_for_chat(chat_id)
            draft = service.repository.get_draft(parts[1])
            if not tenant or not draft or draft.telegram_chat_id != chat_id:
                raise HTTPException(status_code=422, detail="draft callback is not authorized")
            if parts[2] not in {"submit", "add", "undo", "clear", "cancel"}:
                raise HTTPException(status_code=422, detail="unknown draft callback")
            if draft.submitted_incident_id and parts[2] != "submit":
                raise HTTPException(status_code=422, detail="draft was already submitted")
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:draft-button",
                incident_id=f"draft:{draft.draft_id}",
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="button",
                text=parts[2].title(),
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            if parts[2] == "submit":
                try:
                    incident = service.submit_telegram_draft(
                        draft.draft_id, chat_id, provider_message_id
                    )
                except ValueError as exc:
                    service.send_draft_message(
                        draft,
                        str(exc),
                        f"draft-submit-rejected:{draft.revision}",
                        _draft_markup(draft),
                    )
                    kind = "draft_submit_rejected"
                else:
                    service._notify(
                        incident, f"Report submitted as {incident.incident_id}.", "report-submitted"
                    )
                    kind = "draft_submitted"
            elif parts[2] == "cancel":
                service.cancel_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft, "Draft cancelled. No incident was created.", f"draft-cancelled:{draft.revision}"
                )
                kind = "draft_cancelled"
            elif parts[2] == "undo":
                draft = service.undo_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft,
                    "Removed the last report item.\n\n" + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                kind = "draft_undo"
            elif parts[2] == "clear":
                draft = service.clear_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft,
                    "Draft cleared. You can add new text or media.\n\n" + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                kind = "draft_cleared"
            else:
                service.send_draft_message(
                    draft,
                    "Use the message box, 📎 attachment button, or 🎙 microphone below.\n\n"
                    + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                kind = "draft_add_more"
            return {"status": "processed", "kind": kind}

        if len(parts) == 3 and parts[0] == "tenant":
            tenant = _tenant_for_chat(chat_id)
            confirmation = (
                _active_tenant_confirmation(tenant.tenant_id, parts[1]) if tenant else None
            )
            if not tenant or not confirmation or parts[2] not in {"dry", "leaking"}:
                raise HTTPException(status_code=422, detail="unrecognized tenant callback")
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:tenant-button",
                incident_id=confirmation.incident_id,
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="button",
                text="Dry now" if parts[2] == "dry" else "Still leaking",
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            service.process_action(
                confirmation.incident_id,
                ActionRequest(
                    action="tenant_confirm" if parts[2] == "dry" else "recurrence",
                    event_id=f"telegram-action-{update_id}",
                ),
            )
            if hasattr(service.notifications, "answer_callback") and isinstance(
                callback.get("id"), str
            ):
                service.notifications.answer_callback(callback["id"])
            return {
                "status": "processed",
                "kind": "tenant_confirmation" if parts[2] == "dry" else "warranty_recurrence",
            }

        if len(parts) == 3 and parts[0] == "vs":
            vendor = _vendor_for_chat(chat_id)
            session = _vendor_session_for_chat(chat_id, parts[1]) if vendor else None
            if not vendor or not session or session.vendor_id != vendor.vendor_id:
                _answer_callback(callback.get("id"), "This vendor session is no longer current.", True)
                return {"status": "processed", "kind": "stale_vendor_callback"}
            try:
                if parts[2] == "ac":
                    incident = service.get_incident(session.incident_id)
                    if session.stage != "OFFERED" or incident.status.value != "DISPATCHING":
                        raise ValueError("This offer is no longer current.")
                    service.process_action(session.incident_id, ActionRequest(action="vendor_response", event_id=f"telegram-action-{update_id}", payload={"vendor_id": vendor.vendor_id, "outcome": "accept"}))
                    service.accept_vendor_session(session)
                elif parts[2] == "dc":
                    incident = service.get_incident(session.incident_id)
                    if session.stage != "OFFERED" or incident.status.value != "DISPATCHING":
                        raise ValueError("This offer is no longer current.")
                    service.process_action(session.incident_id, ActionRequest(action="vendor_response", event_id=f"telegram-action-{update_id}", payload={"vendor_id": vendor.vendor_id, "outcome": "decline"}))
                    session.stage = "DECLINED"
                    session.cancelled = True
                    service._save_vendor_session(session)
                elif parts[2] == "st":
                    service.start_vendor_job(session, f"telegram-action-{update_id}")
                    service.prepare_completion(session)
                elif parts[2] == "pr":
                    service.begin_completion(session)
                elif parts[2] == "pc":
                    service.confirm_vendor_price(session)
                elif parts[2] == "ec":
                    service.confirm_vendor_eta(session)
                elif parts[2] == "pe":
                    session.stage = "AWAITING_PRICE"
                    service._save_vendor_session(session)
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, "Edit price. Send one SGD amount, for example 220 or PRICE 220.50.", f"vendor-edit-price:{session.session_id}", service._force_reply("SGD amount, e.g. 220.00"))
                elif parts[2] == "ee":
                    session.stage = "AWAITING_ETA"
                    service._save_vendor_session(session)
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, "Edit ETA. Send whole minutes, for example 20 or ETA 20.", f"vendor-edit-eta:{session.session_id}", service._force_reply("Minutes until arrival, e.g. 20"))
                elif parts[2] == "eb":
                    session.stage = "AWAITING_PRICE"
                    session.price_confirmed = False
                    service._save_vendor_session(session)
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, "Back to price. Send one SGD amount.", f"vendor-back-price:{session.session_id}", service._force_reply("SGD amount, e.g. 220.00"))
                elif parts[2] == "su":
                    service.submit_vendor_quote(session, f"telegram-action-{update_id}")
                elif parts[2] == "cx":
                    cancelled_session = service.cancel_vendor_session(session)
                    message = "Intake reset. The job remains reserved. Send one SGD quote to continue." if cancelled_session.stage == "AWAITING_PRICE" else "Completion draft cancelled. Send /complete when the repair is finished." if cancelled_session.stage == "SUBMITTED" else "Offer cancelled."
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, message, f"vendor-session-cancelled:{session.session_id}")
                elif parts[2] == "cr":
                    session.stage = "AWAITING_PHOTO"
                    service._save_vendor_session(session)
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, "Reply with one clear after-photo.", f"completion-photo-replace:{session.session_id}", service._force_reply("Attach one clear after-photo"))
                elif parts[2] == "ce":
                    session.stage = "AWAITING_SCOPE"
                    service._save_vendor_session(session)
                    service._notify_vendor(service.get_incident(session.incident_id), vendor, "Edit the work summary in 10–500 meaningful characters.", f"completion-scope-edit:{session.session_id}", service._force_reply("Describe work performed"))
                elif parts[2] == "cf":
                    service.begin_final_price_edit(session)
                elif parts[2] == "fp":
                    service.confirm_final_price(session, f"telegram-action-{update_id}")
                elif parts[2] == "cs":
                    service.submit_completion(session, f"telegram-action-{update_id}")
                else:
                    raise ValueError("unknown vendor session action")
            except (ValueError, KeyError, IncidentNotFound):
                _answer_callback(callback.get("id"), _vendor_help(session), True)
                return {"status": "processed", "kind": "stale_vendor_callback"}
            service.record_communication(
                communication_id=f"comm:vendor-session:{session.session_id}:button:{parts[2]}:{provider_message_id}",
                incident_id=session.incident_id, sender_role="vendor", sender_id=vendor.vendor_id,
                recipient_role="agent", recipient_id="no-more-buckets", channel="telegram",
                direction="inbound", message_type="button", text=parts[2],
                provider_message_id=provider_message_id, delivery_status="received",
            )
            _answer_callback(callback.get("id"))
            return {"status": "processed", "kind": "vendor_session_callback"}

        vendor = _vendor_for_chat(chat_id)
        if (
            not vendor
            or len(parts) != 3
            or parts[0] != "vendor"
            or parts[2] not in {"accept", "decline", "start"}
        ):
            raise HTTPException(status_code=422, detail="unrecognized Telegram callback")
        try:
            service.get_incident(parts[1])
            session = _vendor_session_for_chat(chat_id, incident_id=parts[1], vendor_id=vendor.vendor_id)
            if not session:
                raise ValueError("This offer is no longer current.")
        except (IncidentNotFound, ValueError):
            _answer_callback(callback.get("id"), "This job is no longer current. Use /status for the current step.", True)
            return {"status": "processed", "kind": "stale_vendor_callback"}
        action: Literal["work_started", "vendor_response"] = (
            "work_started" if parts[2] == "start" else "vendor_response"
        )
        try:
            if parts[2] != "start":
                service.process_action(parts[1], ActionRequest(action=action, event_id=f"telegram-action-{update_id}", payload={"vendor_id": vendor.vendor_id, "outcome": parts[2]}))
                if parts[2] == "accept":
                    service.accept_vendor_session(session)
                else:
                    session.stage = "DECLINED"
                    session.cancelled = True
                    service._save_vendor_session(session)
            else:
                service.start_vendor_job(session, f"telegram-action-{update_id}")
                service.prepare_completion(session)
        except (ValueError, KeyError, IncidentNotFound):
            _answer_callback(callback.get("id"), _vendor_help(session), True)
            return {"status": "processed", "kind": "stale_vendor_callback"}
        service.record_communication(
            communication_id=f"comm:telegram:{parts[1]}:{update_id}:vendor-button",
            incident_id=parts[1], sender_role="vendor", sender_id=vendor.vendor_id,
            recipient_role="agent", recipient_id="no-more-buckets", channel="telegram",
            direction="inbound", message_type="button", text=parts[2].title(),
            provider_message_id=provider_message_id, delivery_status="received",
        )
        _answer_callback(callback.get("id"))
        return {"status": "processed", "kind": "vendor_callback"}

    message = update.get("message")
    if not isinstance(message, dict):
        return {"status": "ignored", "kind": "unsupported"}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text") or message.get("caption") or "").strip()
    provider_message_id = str(message.get("message_id") or update_id)
    tenant = _tenant_for_chat(chat_id)
    vendor = _vendor_for_chat(chat_id)
    command_parts = text.split(maxsplit=1)
    command = command_parts[0].casefold().split("@", 1)[0] if command_parts else ""
    command_argument = command_parts[1] if len(command_parts) == 2 else ""

    if command == "/start":
        if len(command_parts) == 2:
            try:
                record = service.consume_pairing_code(command_parts[1], chat_id, _telegram_sender_id(message))
            except ValueError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if hasattr(service.notifications, "send"):
                service.notifications.send(
                    NotificationMessage(
                        "pairing",
                        chat_id,
                        f"Paired as {record.target_type} {record.target_id}. You can now receive No More Buckets updates.",
                        f"telegram-paired:{chat_id}:{record.target_id}",
                    )
                )
            return {"status": "processed", "kind": "pairing"}
        if not tenant and not vendor:
            raise HTTPException(
                status_code=403, detail="chat is not seeded for this synthetic demo"
            )
        service.mark_telegram_delivery_ready(chat_id)
        service.notifications.send(
            NotificationMessage(
                "onboarding",
                chat_id,
                "Connected. Tenant: send /report to start a draft. Vendors: use the assigned buttons.",
                f"telegram-start:{chat_id}",
            )
        )
        return {"status": "processed", "kind": "start"}

    if tenant:
        if chat_id not in service.started_telegram_chats:
            raise HTTPException(status_code=403, detail="send /start before using this bot")
        confirmation = _active_tenant_confirmation(tenant.tenant_id)
        if confirmation and text.casefold() in {
            "confirm",
            "confirmed",
            "yes",
            "ok",
            "all good",
            "dry now",
        }:
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:tenant-confirm",
                incident_id=confirmation.incident_id,
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="text",
                text=text,
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            service.process_action(
                confirmation.incident_id,
                ActionRequest(action="tenant_confirm", event_id=f"telegram-action-{update_id}"),
            )
            return {"status": "processed", "kind": "tenant_confirmation"}
        if confirmation and text.casefold() in {"still leaking", "leaking", "not fixed"}:
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:tenant-recurrence",
                incident_id=confirmation.incident_id,
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="text",
                text=text,
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            service.process_action(
                confirmation.incident_id,
                ActionRequest(action="recurrence", event_id=f"telegram-action-{update_id}"),
            )
            return {"status": "processed", "kind": "warranty_recurrence"}

        draft = service.get_active_telegram_draft(chat_id)
        if command == "/report":
            draft = service.create_telegram_draft(tenant, chat_id)
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:report-command",
                incident_id=f"draft:{draft.draft_id}",
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="text",
                text="/report",
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            service.send_draft_message(
                draft, service.draft_summary(draft), service.draft_summary_action_key(draft), _draft_markup(draft)
            )
            return {"status": "processed", "kind": "draft_started"}
        if command in {"/cancel", "/submit", "/add", "/undo", "/clear"}:
            if not draft:
                service.notifications.send(
                    NotificationMessage(
                        f"chat:{chat_id}",
                        chat_id,
                        "There is no active report draft. Send /report to begin.",
                        f"telegram-no-draft:{chat_id}:{command}",
                    )
                )
                return {"status": "processed", "kind": "no_active_draft"}
            if command == "/cancel":
                service.record_communication(
                    communication_id=f"comm:telegram:{update_id}:draft-command",
                    incident_id=f"draft:{draft.draft_id}",
                    sender_role="tenant",
                    sender_id=tenant.tenant_id,
                    recipient_role="agent",
                    recipient_id="no-more-buckets",
                    channel="telegram",
                    direction="inbound",
                    message_type="text",
                    text=text,
                    provider_message_id=provider_message_id,
                    delivery_status="received",
                )
                service.send_draft_message(
                    draft, "Draft cancelled. No incident was created.", f"draft-cancelled:{draft.revision}"
                )
                service.cancel_telegram_draft(draft.draft_id, chat_id)
                return {"status": "processed", "kind": "draft_cancelled"}
            if command == "/add":
                service.record_communication(
                    communication_id=f"comm:telegram:{update_id}:draft-command",
                    incident_id=f"draft:{draft.draft_id}",
                    sender_role="tenant",
                    sender_id=tenant.tenant_id,
                    recipient_role="agent",
                    recipient_id="no-more-buckets",
                    channel="telegram",
                    direction="inbound",
                    message_type="text",
                    text=text,
                    provider_message_id=provider_message_id,
                    delivery_status="received",
                )
                service.send_draft_message(
                    draft,
                    "Use the message box, 📎 attachment button, or 🎙 microphone below.\n\n"
                    + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                return {"status": "processed", "kind": "draft_add_more"}
            if command == "/undo":
                draft = service.undo_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft,
                    "Removed the last report item.\n\n" + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                return {"status": "processed", "kind": "draft_undo"}
            if command == "/clear":
                draft = service.clear_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft,
                    "Draft cleared. You can add new text or media.\n\n" + service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                return {"status": "processed", "kind": "draft_cleared"}
            service.record_communication(
                communication_id=f"comm:telegram:{update_id}:draft-command",
                incident_id=f"draft:{draft.draft_id}",
                sender_role="tenant",
                sender_id=tenant.tenant_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel="telegram",
                direction="inbound",
                message_type="text",
                text=text,
                provider_message_id=provider_message_id,
                delivery_status="received",
            )
            try:
                incident = service.submit_telegram_draft(draft.draft_id, chat_id, provider_message_id)
            except ValueError as exc:
                service.send_draft_message(
                    draft, str(exc), f"draft-submit-rejected:{draft.revision}", _draft_markup(draft)
                )
                return {"status": "processed", "kind": "draft_submit_rejected"}
            service._notify(
                incident, f"Report submitted as {incident.incident_id}.", "report-submitted"
            )
            return {"status": "processed", "kind": "draft_submitted"}
        if draft:
            item_key = _draft_item_key(update_id, message)
            if item_key in draft.item_keys:
                service.send_draft_message(
                    draft,
                    service.draft_summary(draft),
                    service.draft_summary_action_key(draft),
                    _draft_markup(draft),
                )
                return {"status": "processed", "kind": "draft_duplicate_item"}
            media = _telegram_media(message, "tenant")
            draft = service.append_telegram_draft(
                draft.draft_id,
                chat_id,
                text=text,
                media=media,
                communication_id=f"comm:telegram:{update_id}:draft-update",
                provider_message_id=provider_message_id,
                item_key=item_key,
            )
            service.send_draft_message(
                draft, service.draft_summary(draft), service.draft_summary_action_key(draft), _draft_markup(draft)
            )
            return {"status": "processed", "kind": "draft_update"}
        service.notifications.send(
            NotificationMessage(
                f"chat:{chat_id}",
                chat_id,
                "Send /report to start a report draft, then add text, photos, or a voice note.",
                f"telegram-report-required:{chat_id}",
            )
        )
        return {"status": "processed", "kind": "report_command_required"}

    if vendor:
        if not _authorized_vendor_sender(vendor, _telegram_sender_id(message)):
            return {"status": "ignored", "kind": "vendor_sender_unauthorized"}
        vendor_incident = _active_vendor_incident(vendor.vendor_id)
        if not vendor_incident:
            service.notifications.send(NotificationMessage("vendor", chat_id, "There is no active vendor work. Use /help for instructions.", f"vendor-no-active:{chat_id}"))
            return {"status": "processed", "kind": "no_active_vendor_work"}
        session = _vendor_session_for_chat(chat_id, incident_id=vendor_incident.incident_id, vendor_id=vendor.vendor_id)
        if not session:
            service.notifications.send(NotificationMessage(vendor_incident.incident_id, chat_id, "I can’t identify one active vendor session. Use /status from the current work-order message.", f"vendor-session-ambiguous:{chat_id}"))
            return {"status": "processed", "kind": "vendor_session_required"}
        media = _telegram_media(message, "vendor")
        service.record_communication(
            communication_id=f"comm:telegram:{vendor_incident.incident_id}:{update_id}:{provider_message_id}:{','.join(asset.asset_id for asset in media) or 'text'}",
            incident_id=vendor_incident.incident_id,
            sender_role="vendor",
            sender_id=vendor.vendor_id,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel="telegram",
            direction="inbound",
            message_type=(
                "video"
                if any(asset.mime_type.startswith("video/") for asset in media)
                else "image"
                if media
                else "text"
            ),
            text=text or "Vendor submitted a Telegram update.",
            media_ids=[asset.asset_id for asset in media],
            provider_message_id=provider_message_id,
            delivery_status="received",
        )
        if command in {"/help", "/status"}:
            service._notify_vendor(vendor_incident, vendor, _vendor_help(session), f"vendor-{command[1:]}:{session.session_id}")
            return {"status": "processed", "kind": f"vendor_{command[1:]}"}
        if command == "/cancel":
            try:
                cancelled_session = service.cancel_vendor_session(session)
            except ValueError:
                service._notify_vendor(vendor_incident, vendor, _vendor_help(session), f"vendor-cancel-invalid:{session.session_id}:{session.revision}")
                return {"status": "processed", "kind": "vendor_step_help"}
            message = "Intake reset. The job remains reserved. Send one SGD quote to continue." if cancelled_session.stage == "AWAITING_PRICE" else "Completion draft cancelled. Send /complete when the repair is finished." if cancelled_session.stage == "SUBMITTED" else "Offer cancelled."
            service._notify_vendor(vendor_incident, vendor, message, f"vendor-cancel:{session.session_id}")
            return {"status": "processed", "kind": "vendor_cancelled"}
        if command == "/complete":
            try:
                service.begin_completion(session)
            except ValueError:
                service._notify_vendor(vendor_incident, vendor, _vendor_help(session), f"vendor-complete-invalid:{session.session_id}:{session.revision}")
                return {"status": "processed", "kind": "vendor_step_help"}
            return {"status": "processed", "kind": "vendor_completion_started"}
        if session.stage == "AWAITING_PHOTO" and media:
            service.completion_photo(session, media)
            return {"status": "processed", "kind": "completion_photo"}
        if session.stage == "AWAITING_SCOPE":
            service.completion_scope(session, text)
            return {"status": "processed", "kind": "completion_scope"}
        if session.stage == "CONFIRMING_FINAL_PRICE":
            amount = parse_telegram_price(text)
            if amount is None:
                service._notify_vendor(vendor_incident, vendor, "❌ I couldn’t use that final price. Send one SGD amount with at most two decimal places.", f"completion-price-invalid:{session.session_id}:{session.revision}", service._force_reply("Final SGD price, e.g. 220.00"))
            else:
                service.change_final_price(session, amount)
            return {"status": "processed", "kind": "completion_final_price"}
        intake_text = f"PRICE {command_argument}" if command == "/price" else f"ETA {command_argument}" if command == "/eta" else text
        if session.stage in {"AWAITING_PRICE", "CONFIRMING_PRICE"}:
            legacy = parse_telegram_legacy_vendor_input(intake_text)
            if legacy:
                service.legacy_vendor_input(session, legacy[0], legacy[1])
                return {"status": "processed", "kind": "vendor_legacy_draft"}
            amount = parse_telegram_price(intake_text)
            if amount is None:
                service._notify_vendor(vendor_incident, vendor, "❌ I couldn’t use that price.\n\nSend one SGD amount, for example:\n220\nPRICE 220.50\n\nThe S$250 autonomous limit still applies.", f"vendor-price-invalid:{session.session_id}:{session.revision}", service._force_reply("SGD amount, e.g. 220.00"))
            else:
                service.vendor_price(session, amount)
            return {"status": "processed", "kind": "vendor_price"}
        if session.stage in {"AWAITING_ETA", "CONFIRMING_ETA"}:
            minutes = parse_telegram_eta(intake_text)
            if minutes is None:
                service._notify_vendor(vendor_incident, vendor, "❌ I couldn’t use that arrival ETA.\n\nSend a whole number from 1 to 1440 minutes, for example:\n20\nETA 20\n20 minutes", f"vendor-eta-invalid:{session.session_id}:{session.revision}", service._force_reply("Minutes until arrival, e.g. 20"))
            else:
                service.vendor_eta(session, minutes)
            return {"status": "processed", "kind": "vendor_eta"}
        service._notify_vendor(vendor_incident, vendor, _vendor_help(session), f"vendor-step-help:{session.session_id}:{session.revision}")
        return {"status": "processed", "kind": "vendor_step_help"}
    raise HTTPException(
        status_code=403, detail="Telegram chat is not seeded for this synthetic demo"
    )


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
