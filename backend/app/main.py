from __future__ import annotations

import base64
import hmac
import json
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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
    Incident,
    PairingCodeRequest,
    PairingCodeResponse,
    ReportInput,
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
        vendors_adapter = DemoTelegramVendorAdapter(
            notifications, settings.demo_vendor_a_behavior
        )
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
    return next((tenant for tenant in service.tenants.values() if tenant.telegram_chat_id == chat_id), None)


def _vendor_for_chat(chat_id: str):
    return next((vendor for vendor in service.vendors if vendor.telegram_chat_id == chat_id), None)


def _active_vendor_incident(vendor_id: str) -> Incident | None:
    matches = [
        incident
        for incident in service.list_incidents()
        if incident.assigned_vendor_id == vendor_id
        or any(
            attempt.vendor_id == vendor_id
            for attempt in incident.vendor_attempts
        )
    ]
    return max(matches, key=lambda incident: incident.updated_at) if matches else None


def _telegram_media(message: dict) -> list:
    if not isinstance(service.notifications, TelegramBotAdapter):
        raise HTTPException(status_code=503, detail="Telegram adapter is not active")
    media = []
    photos = message.get("photo", [])
    if isinstance(photos, list) and photos:
        photo = photos[-1]
        if isinstance(photo, dict) and isinstance(photo.get("file_id"), str):
            media.append(
                service.notifications.download_media(
                    photo["file_id"], mime_type="image/jpeg", filename="telegram-photo.jpg"
                )
            )
    voice = message.get("voice")
    if isinstance(voice, dict) and isinstance(voice.get("file_id"), str):
        media.append(
            service.notifications.download_media(
                voice["file_id"], mime_type="audio/ogg", filename="telegram-voice.ogg"
            )
        )
    return media


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
    _require_demo_api()
    return service.list_incidents()


@app.get("/api/incidents/{incident_id}")
def get_incident(incident_id: str) -> Incident:
    _require_demo_api()
    try:
        return service.get_incident(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail="incident not found") from exc


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
        media=[media],
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
    try:
        if task_type == "tenant_confirmation":
            service.process_action(
                incident_id,
                ActionRequest(action="tenant_confirm", event_id=f"task-{task_id}"),
            )
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
    telegram_secret: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
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
    webhook_event_id = f"telegram-webhook-{update_id}"
    if not service.repository.claim_event(webhook_event_id):
        return {"status": "duplicate", "kind": "telegram_update"}
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", "")) if isinstance(message, dict) else ""
        vendor = _vendor_for_chat(chat_id)
        callback_data = callback.get("data", "")
        parts = callback_data.split(":") if isinstance(callback_data, str) else []
        if not vendor or len(parts) != 3 or parts[0] != "vendor" or parts[2] not in {"accept", "decline"}:
            raise HTTPException(status_code=422, detail="unrecognized vendor callback")
        service.process_action(
            parts[1],
            ActionRequest(
                action="vendor_response",
                event_id=f"telegram-update-{update_id}",
                payload={"vendor_id": vendor.vendor_id, "outcome": parts[2]},
            ),
        )
        if isinstance(service.notifications, TelegramBotAdapter) and isinstance(callback.get("id"), str):
            service.notifications.answer_callback(callback["id"])
        return {"status": "processed", "kind": "vendor_callback"}

    message = update.get("message")
    if not isinstance(message, dict):
        return {"status": "ignored", "kind": "unsupported"}
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = str(message.get("text") or message.get("caption") or "").strip()
    tenant = _tenant_for_chat(chat_id)
    vendor = _vendor_for_chat(chat_id)
    start_parts = text.split(maxsplit=1)
    if start_parts and start_parts[0].lower() == "/start":
        if len(start_parts) == 2:
            try:
                record = service.consume_pairing_code(start_parts[1], chat_id)
            except ValueError as exc:
                raise HTTPException(status_code=403, detail=str(exc)) from exc
            if isinstance(service.notifications, TelegramBotAdapter):
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
            raise HTTPException(status_code=403, detail="chat is not seeded for this synthetic demo")
        service.started_telegram_chats.add(chat_id)
        if isinstance(service.notifications, TelegramBotAdapter):
            service.notifications.send(
                NotificationMessage(
                    "onboarding",
                    chat_id,
                    "Connected. Send a leak description, photo, or voice note. Vendors can use assigned buttons.",
                    f"telegram-start:{chat_id}",
                )
            )
        return {"status": "processed", "kind": "start"}
    if tenant:
        media = _telegram_media(message)
        report_text = text or "Tenant submitted a photo or voice note about a possible plumbing issue."
        service.submit_report(
            ReportInput(
                property_id=tenant.property_id,
                tenant_id=tenant.tenant_id,
                report_text=report_text,
                media=media,
                idempotency_key=f"telegram-update-{update_id}",
            )
        )
        return {"status": "processed", "kind": "tenant_report"}
    if vendor:
        incident = _active_vendor_incident(vendor.vendor_id)
        if not incident:
            return {"status": "ignored", "kind": "no_active_vendor_work"}
        reply = parse_telegram_vendor_reply(text)
        if not reply:
            return {"status": "ignored", "kind": "unrecognized_vendor_text"}
        action_event_id = f"telegram-action-{update_id}"
        if reply.get("outcome") in {"accept", "decline"}:
            service.process_action(
                incident.incident_id,
                ActionRequest(
                    action="vendor_response",
                    event_id=action_event_id,
                    payload={
                        "vendor_id": vendor.vendor_id,
                        "outcome": reply["outcome"],
                    },
                ),
            )
            action_event_id = f"telegram-action-{update_id}-quote"
        if "amount" in reply:
            service.process_action(
                incident.incident_id,
                ActionRequest(
                    action="vendor_quote",
                    event_id=action_event_id,
                    payload={"vendor_id": vendor.vendor_id, "amount": reply["amount"]},
                ),
            )
        if "eta_minutes" in reply:
            service.process_action(
                incident.incident_id,
                ActionRequest(
                    action="eta",
                    event_id=f"telegram-action-{update_id}-eta",
                    payload={"vendor_id": vendor.vendor_id, "eta_minutes": reply["eta_minutes"]},
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
    raise HTTPException(status_code=403, detail="Telegram chat is not seeded for this synthetic demo")


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if frontend_dist.exists():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
