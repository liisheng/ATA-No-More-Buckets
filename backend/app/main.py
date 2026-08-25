from __future__ import annotations

import base64
import hmac
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
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
    TaskQueue,
    TelegramBotAdapter,
    TelegramVendorAdapter,
    TwilioWhatsAppAdapter,
    VendorAdapter,
    VertexGeminiFactExtractor,
    build_demo_media,
    build_demo_voice_media,
    parse_telegram_completion,
    parse_telegram_vendor_reply,
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
    Invoice,
    InvoiceLineItem,
    MediaDescriptor,
    PairingCodeRequest,
    PairingCodeResponse,
    ReportInput,
    TelegramDraft,
    TenantContact,
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
        clock=DemoClock() if settings.demo_mode else None,
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


def _tenant_for_chat(chat_id: str) -> TenantContact | None:
    return next(
        (tenant for tenant in service.tenants.values() if tenant.telegram_chat_id == chat_id), None
    )


def _vendor_for_chat(chat_id: str):
    return next((vendor for vendor in service.vendors if vendor.telegram_chat_id == chat_id), None)


def _active_vendor_incident(vendor_id: str) -> Incident | None:
    matches = [
        incident
        for incident in service.list_incidents()
        if incident.assigned_vendor_id == vendor_id
        or any(attempt.vendor_id == vendor_id for attempt in incident.vendor_attempts)
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
            )
        )
    return media


def _draft_markup(draft: TelegramDraft) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Submit report", "callback_data": f"draft:{draft.draft_id}:submit"},
                {"text": "Add more", "callback_data": f"draft:{draft.draft_id}:add"},
            ],
            [{"text": "Cancel", "callback_data": f"draft:{draft.draft_id}:cancel"}],
        ]
    }


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
                    source=asset.source,
                    url=f"/api/incidents/{incident_id}/media/{asset.asset_id}",
                )
            )
    return descriptors


@app.get("/api/incidents/{incident_id}/media/{media_id}")
def get_media(incident_id: str, media_id: str) -> Response:
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
    return Response(
        content=content,
        media_type=asset.mime_type,
        headers={
            "Content-Disposition": f'inline; filename="{filename.replace(chr(34), "")}"',
            "Cache-Control": "private, max-age=60",
        },
    )


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
    _require_demo_api()
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
    _require_demo_api()
    global service
    service = create_service(settings)
    return JSONResponse({"status": "reset"})


@app.post("/api/events/tasks")
def task_event(payload: dict) -> dict[str, str]:
    # The stable task ID becomes the action event ID; repeated Cloud Tasks delivery is a no-op.
    task_id = str(payload.get("task_id", "unknown"))
    task_type = str(payload.get("task_type", ""))
    incident_id = str(payload.get("incident_id", ""))
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
                    payload={"vendor_id": payload.get("payload", {}).get("vendor_id", "")},
                ),
            )
        elif task_type == "vendor_retry":
            service.process_action(
                incident_id,
                ActionRequest(
                    action="vendor_retry",
                    event_id=f"task-{task_id}",
                    payload={"vendor_id": payload.get("payload", {}).get("vendor_id", "")},
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

        if len(parts) == 3 and parts[0] == "draft":
            tenant = _tenant_for_chat(chat_id)
            draft = service.repository.get_draft(parts[1])
            if not tenant or not draft or draft.telegram_chat_id != chat_id:
                raise HTTPException(status_code=422, detail="draft callback is not authorized")
            if parts[2] not in {"submit", "add", "cancel"}:
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
                incident = service.submit_telegram_draft(
                    draft.draft_id, chat_id, provider_message_id
                )
                service._notify(
                    incident, f"Report submitted as {incident.incident_id}.", "report-submitted"
                )
                kind = "draft_submitted"
            elif parts[2] == "cancel":
                service.cancel_telegram_draft(draft.draft_id, chat_id)
                service.send_draft_message(
                    draft, "Draft cancelled. No incident was created.", "draft-cancelled"
                )
                kind = "draft_cancelled"
            else:
                service.send_draft_message(
                    draft, service.draft_summary(draft), "draft-summary", _draft_markup(draft)
                )
                kind = "draft_add_more"
            if hasattr(service.notifications, "answer_callback") and isinstance(
                callback.get("id"), str
            ):
                service.notifications.answer_callback(callback["id"])
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

        vendor = _vendor_for_chat(chat_id)
        if (
            not vendor
            or len(parts) != 3
            or parts[0] != "vendor"
            or parts[2] not in {"accept", "decline", "start"}
        ):
            raise HTTPException(status_code=422, detail="unrecognized Telegram callback")
        try:
            callback_incident = service.get_incident(parts[1])
        except IncidentNotFound as exc:
            raise HTTPException(status_code=422, detail="callback incident is unavailable") from exc
        if not (
            callback_incident.assigned_vendor_id == vendor.vendor_id
            or any(
                attempt.vendor_id == vendor.vendor_id
                for attempt in callback_incident.vendor_attempts
            )
        ):
            raise HTTPException(status_code=422, detail="vendor is not assigned to this incident")
        action: Literal["work_started", "vendor_response"] = (
            "work_started" if parts[2] == "start" else "vendor_response"
        )
        service.record_communication(
            communication_id=f"comm:telegram:{update_id}:vendor-button",
            incident_id=parts[1],
            sender_role="vendor",
            sender_id=vendor.vendor_id,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel="telegram",
            direction="inbound",
            message_type="button",
            text=parts[2].title(),
            provider_message_id=provider_message_id,
            delivery_status="received",
        )
        service.process_action(
            parts[1],
            ActionRequest(
                action=action,
                event_id=f"telegram-action-{update_id}",
                payload=(
                    {"vendor_id": vendor.vendor_id, "outcome": parts[2]}
                    if action == "vendor_response"
                    else {"vendor_id": vendor.vendor_id}
                ),
            ),
        )
        if hasattr(service.notifications, "answer_callback") and isinstance(
            callback.get("id"), str
        ):
            service.notifications.answer_callback(callback["id"])
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
    command = command_parts[0].casefold() if command_parts else ""

    if command == "/start":
        if len(command_parts) == 2:
            try:
                record = service.consume_pairing_code(command_parts[1], chat_id)
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
        service.started_telegram_chats.add(chat_id)
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
                draft, service.draft_summary(draft), "draft-summary", _draft_markup(draft)
            )
            return {"status": "processed", "kind": "draft_started"}
        if command in {"/cancel", "/submit", "/add"}:
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
                    draft, "Draft cancelled. No incident was created.", "draft-cancelled"
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
                    draft, service.draft_summary(draft), "draft-summary", _draft_markup(draft)
                )
                return {"status": "processed", "kind": "draft_add_more"}
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
            incident = service.submit_telegram_draft(draft.draft_id, chat_id, provider_message_id)
            service._notify(
                incident, f"Report submitted as {incident.incident_id}.", "report-submitted"
            )
            return {"status": "processed", "kind": "draft_submitted"}
        if draft:
            media = _telegram_media(message, "tenant")
            draft = service.append_telegram_draft(
                draft.draft_id,
                chat_id,
                text=text,
                media=media,
                communication_id=f"comm:telegram:{update_id}:draft-update",
                provider_message_id=provider_message_id,
            )
            service.send_draft_message(
                draft, service.draft_summary(draft), "draft-summary", _draft_markup(draft)
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
        vendor_incident = _active_vendor_incident(vendor.vendor_id)
        if not vendor_incident:
            return {"status": "ignored", "kind": "no_active_vendor_work"}
        media = _telegram_media(message, "vendor")
        completion = parse_telegram_completion(text)
        service.record_communication(
            communication_id=f"comm:telegram:{update_id}:vendor-message",
            incident_id=vendor_incident.incident_id,
            sender_role="vendor",
            sender_id=vendor.vendor_id,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel="telegram",
            direction="inbound",
            message_type="image" if media else "text",
            text=text or "Vendor submitted a Telegram update.",
            media_ids=[asset.asset_id for asset in media],
            provider_message_id=provider_message_id,
            delivery_status="received",
        )
        if completion:
            photo = next((asset for asset in media if asset.mime_type.startswith("image/")), None)
            if not photo:
                service._notify_vendor(
                    vendor_incident,
                    vendor,
                    "Completion details received. Please attach the after-photo with the same COMPLETE / PRICE / SCOPE caption.",
                    "completion-photo-required",
                )
                return {"status": "processed", "kind": "completion_photo_required"}
            invoice = Invoice(
                invoice_id=f"invoice_{vendor_incident.incident_id}_{update_id}",
                vendor_id=vendor.vendor_id,
                currency=vendor_incident.work_order.currency
                if vendor_incident.work_order
                else service.settings.currency,
                total=completion["amount"],
                line_items=[
                    InvoiceLineItem(
                        description=completion["scope"], quantity=1, unit_price=completion["amount"]
                    )
                ],
            )
            service.process_action(
                vendor_incident.incident_id,
                ActionRequest(
                    action="completion",
                    event_id=f"telegram-action-{update_id}",
                    payload={"photo": photo.model_dump(), "invoice": invoice.model_dump()},
                ),
            )
            return {"status": "processed", "kind": "completion_evidence"}
        if text.upper().startswith("COMPLETE"):
            service._notify_vendor(
                vendor_incident,
                vendor,
                "Use the exact three-line caption COMPLETE / PRICE <amount> / SCOPE <work performed> and attach an after-photo.",
                "completion-format-required",
            )
            return {"status": "processed", "kind": "completion_format_required"}
        reply = parse_telegram_vendor_reply(text)
        if not reply:
            return {"status": "ignored", "kind": "unrecognized_vendor_text"}
        action_event_id = f"telegram-action-{update_id}"
        if reply.get("outcome") in {"accept", "decline"}:
            service.process_action(
                vendor_incident.incident_id,
                ActionRequest(
                    action="vendor_response",
                    event_id=action_event_id,
                    payload={"vendor_id": vendor.vendor_id, "outcome": reply["outcome"]},
                ),
            )
            action_event_id = f"telegram-action-{update_id}-quote"
        if "amount" in reply:
            service.process_action(
                vendor_incident.incident_id,
                ActionRequest(
                    action="vendor_quote",
                    event_id=action_event_id,
                    payload={"vendor_id": vendor.vendor_id, "amount": reply["amount"]},
                ),
            )
            if "eta_minutes" in reply:
                service.process_action(
                    vendor_incident.incident_id,
                    ActionRequest(
                        action="eta",
                        event_id=f"telegram-action-{update_id}-eta",
                        payload={
                            "vendor_id": vendor.vendor_id,
                            "eta_minutes": reply["eta_minutes"],
                        },
                    ),
                )
        kinds = []
        if reply.get("outcome"):
            kinds.append(f"vendor_{reply['outcome']}")
        if "amount" in reply:
            kinds.append("vendor_quote")
        if "eta_minutes" in reply:
            kinds.append("vendor_eta")
        return {"status": "processed", "kind": "+".join(kinds)}
    raise HTTPException(
        status_code=403, detail="Telegram chat is not seeded for this synthetic demo"
    )


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
