from __future__ import annotations

import secrets
import threading
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import structlog

from .adapters import (
    Clock,
    CompletionEvidenceVerifier,
    DemoClock,
    EventBus,
    FactExtractor,
    MediaStore,
    NotificationAdapter,
    NotificationMessage,
    SystemClock,
    TaskQueue,
    VendorAdapter,
    sanitize_contact_text,
    validate_media_asset,
)
from .config import Settings
from .models import (
    ActionRequest,
    ApprovalRequest,
    CommunicationRecord,
    CompletionEvidence,
    Incident,
    IncidentStatus,
    Invoice,
    InvoiceLineItem,
    PairingCodeRecord,
    PairingCodeResponse,
    PropertyConfig,
    ReportAssessment,
    ReportInput,
    RuntimeMetadata,
    TelegramDraft,
    TelegramDraftItem,
    TenantContact,
    TimelineEntry,
    Vendor,
    VendorAttempt,
    VendorSession,
)
from .policies import (
    assess_completion,
    build_bounded_work_order,
    evaluate_safety,
    property_specific_containment,
    rank_eligible_vendors,
    requires_spending_approval,
)
from .repositories import IncidentRepository
from .state_machine import validate_transition

log = structlog.get_logger(__name__)

ContactRole = Literal["tenant", "agent", "vendor", "scheduler", "system"]
ContactMessageType = Literal["text", "image", "video", "audio", "button", "invoice", "system"]


def _safe_contact_text(value: str | None, max_length: int = 4000) -> str:
    return sanitize_contact_text(value, max_length)


def format_tenant_eta(
    eta: datetime,
    duration_minutes: int | None,
    display_timezone: str,
    now: datetime | None = None,
) -> str:
    """Format an arrival for tenants without exposing UTC or ISO timestamps."""

    local_eta = eta.astimezone(ZoneInfo(display_timezone))
    hour = local_eta.hour % 12 or 12
    clock = f"{hour}:{local_eta.minute:02d} {'AM' if local_eta.hour < 12 else 'PM'}"
    today = (now or datetime.now(UTC)).astimezone(ZoneInfo(display_timezone)).date()
    if local_eta.date() == today:
        day = ""
    elif local_eta.date() == today + timedelta(days=1):
        day = "Tomorrow, "
    else:
        day = f"{local_eta:%b} {local_eta.day}, "
    duration = f" ({duration_minutes} minutes)" if duration_minutes is not None else ""
    return f"{day}{clock}{duration}"


class IncidentNotFound(KeyError):
    pass


class IncidentService:
    def __init__(
        self,
        settings: Settings,
        repository: IncidentRepository,
        extractor: FactExtractor,
        notifications: NotificationAdapter,
        vendors_adapter: VendorAdapter,
        evidence_verifier: CompletionEvidenceVerifier,
        media_store: MediaStore,
        event_bus: EventBus,
        tasks: TaskQueue,
        properties: dict[str, PropertyConfig],
        vendors: list[Vendor],
        tenants: dict[str, TenantContact],
        clock: Clock | None = None,
        agent: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.extractor = extractor
        self.notifications = notifications
        self.vendors_adapter = vendors_adapter
        self.evidence_verifier = evidence_verifier
        self.media_store = media_store
        self.event_bus = event_bus
        self.tasks = tasks
        self.properties = properties
        self.vendors = vendors
        self.tenants = tenants
        self.clock = clock or SystemClock()
        self.agent = agent
        self.started_telegram_chats: set[str] = set()
        self._local_task_timers: dict[str, threading.Timer] = {}
        self._closed = False
        self.live_cloud = bool(settings.k_service) or repository.provider_name in {
            "firestore",
            "cloud_storage",
        }
        for contact in self.tenants.values():
            if contact.telegram_chat_id and (
                contact.delivery_ready or self.live_cloud
            ):
                self.started_telegram_chats.add(contact.telegram_chat_id)
        for vendor in self.vendors:
            if vendor.telegram_chat_id and (vendor.delivery_ready or self.live_cloud):
                self.started_telegram_chats.add(vendor.telegram_chat_id)

    def shutdown(self) -> None:
        """Stop local replay timers before a service instance is replaced."""

        self._closed = True
        for timer in self._local_task_timers.values():
            timer.cancel()
        self._local_task_timers.clear()

    def _enqueue_task(
        self,
        task_id: str,
        incident_id: str,
        task_type: str,
        payload: dict[str, Any],
        delay_seconds: int,
    ) -> str:
        if self._closed:
            return f"cancelled:{task_id}"
        result = self.tasks.enqueue(task_id, incident_id, task_type, payload, delay_seconds)
        if (
            self.settings.demo_mode
            and self.settings.app_env == "demo"
            and self.tasks.provider_name == "local_tasks"
            and task_id not in self._local_task_timers
        ):
            timer = threading.Timer(
                max(0, delay_seconds),
                self._run_local_task,
                args=(task_id, incident_id, task_type, payload),
            )
            timer.daemon = True
            self._local_task_timers[task_id] = timer
            timer.start()
        return result

    def _run_local_task(
        self, task_id: str, incident_id: str, task_type: str, payload: dict[str, Any]
    ) -> None:
        if self._closed:
            self._local_task_timers.pop(task_id, None)
            return
        self.record_communication(
            communication_id=f"comm:scheduler:{task_id}",
            incident_id=incident_id,
            sender_role="scheduler",
            sender_id=self.tasks.provider_name,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel=self.tasks.provider_name,
            direction="inbound",
            message_type="system",
            text=f"Scheduler delivered {task_type}.",
            provider_message_id=task_id,
            delivery_status="simulated",
        )
        try:
            if task_type == "tenant_confirmation":
                self.request_tenant_confirmation(incident_id, event_id=f"task-{task_id}")
            elif task_type == "vendor_timeout":
                if isinstance(self.clock, DemoClock):
                    persisted_incident = self.repository.get(incident_id)
                    pending_attempt = next(
                        (
                            attempt
                            for attempt in (persisted_incident.vendor_attempts if persisted_incident else [])
                            if attempt.vendor_id == payload.get("vendor_id") and attempt.outcome == "pending"
                        ),
                        None,
                    )
                    if pending_attempt and pending_attempt.deadline_at and self.clock.now() < pending_attempt.deadline_at:
                        self.clock.current = pending_attempt.deadline_at
                self.process_action(
                    incident_id,
                    ActionRequest(
                        action="vendor_timeout",
                        event_id=f"task-{task_id}",
                        payload={"vendor_id": payload.get("vendor_id", "")},
                    ),
                )
            elif task_type == "vendor_retry":
                self.process_action(
                    incident_id,
                    ActionRequest(
                        action="vendor_retry",
                        event_id=f"task-{task_id}",
                        payload={"vendor_id": payload.get("vendor_id", "")},
                    ),
                )
        finally:
            self._local_task_timers.pop(task_id, None)

    def _draft_now(self) -> datetime:
        # Draft expiry is a user-input safety boundary, not a demo-clock event.
        return datetime.now(UTC)

    def get_active_telegram_draft(self, telegram_chat_id: str) -> TelegramDraft | None:
        for draft in self.repository.list_drafts(telegram_chat_id):
            if self._draft_now() >= draft.expires_at:
                self.repository.delete_draft(draft.draft_id)
                continue
            if draft.submitted_incident_id:
                continue
            return draft
        return None

    def list_active_telegram_drafts(self) -> list[TelegramDraft]:
        active: list[TelegramDraft] = []
        for draft in self.repository.list_all_drafts():
            if self._draft_now() >= draft.expires_at:
                self.repository.delete_draft(draft.draft_id)
                continue
            if not draft.submitted_incident_id:
                active.append(draft)
        return active

    def list_draft_communications(self, draft_id: str) -> list[CommunicationRecord]:
        draft = self.repository.get_draft(draft_id)
        if not draft:
            raise ValueError("draft not found")
        return self.repository.list_communications(f"draft:{draft_id}")

    def create_telegram_draft(self, tenant: TenantContact, telegram_chat_id: str) -> TelegramDraft:
        existing = self.get_active_telegram_draft(telegram_chat_id)
        if existing:
            return existing
        now = self._draft_now()
        draft = TelegramDraft(
            draft_id=f"draft_{uuid4().hex[:12]}",
            tenant_id=tenant.tenant_id,
            property_id=tenant.property_id,
            telegram_chat_id=telegram_chat_id,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.settings.telegram_draft_expiry_seconds),
        )
        self.repository.save_draft(draft)
        return draft

    def append_telegram_draft(
        self,
        draft_id: str,
        telegram_chat_id: str,
        *,
        text: str,
        media: list,
        communication_id: str,
        provider_message_id: str | None,
        item_key: str | None = None,
    ) -> TelegramDraft:
        draft = self.repository.get_draft(draft_id)
        if not draft or draft.telegram_chat_id != telegram_chat_id:
            raise ValueError("Telegram draft is unavailable")
        if draft.submitted_incident_id:
            raise ValueError("Telegram draft was already submitted")
        if self._draft_now() >= draft.expires_at:
            self.repository.delete_draft(draft_id)
            raise ValueError("Telegram draft has expired; send /report to start again")
        resolved_item_key = item_key or communication_id
        if resolved_item_key in draft.item_keys:
            return draft
        clean_text = _safe_contact_text(text)
        if clean_text:
            draft.text_parts.append(clean_text)
        stored_media = []
        for asset in media:
            validate_media_asset(asset)
            asset_copy = asset.model_copy(deep=True)
            asset_copy.storage_uri = self.media_store.put(asset_copy)
            # Draft records keep metadata and storage ownership; bytes are fetched
            # back from the media store when the draft is submitted.
            asset_copy.content_base64 = None
            stored_media.append(asset_copy)
            draft.media.append(asset_copy)
        item_kind: Literal["text", "image", "video", "audio"] = "text"
        if stored_media:
            first_mime = stored_media[0].mime_type.lower()
            item_kind = (
                "audio"
                if first_mime.startswith("audio/")
                else "video"
                if first_mime.startswith("video/")
                else "image"
            )
        draft.items.append(
            TelegramDraftItem(
                item_key=resolved_item_key,
                kind=item_kind if stored_media else "text",
                text=clean_text,
                media_id=stored_media[0].asset_id if stored_media else None,
                communication_id=communication_id,
            )
        )
        draft.item_keys.append(resolved_item_key)
        draft.updated_at = self._draft_now()
        draft.revision += 1
        draft.communication_ids.append(communication_id)
        self.repository.save_draft(draft)
        if media:
            message_type: ContactMessageType = (
                "audio" if any(asset.mime_type.startswith("audio/") for asset in media) else "image"
            )
            if any(asset.mime_type.startswith("video/") for asset in media):
                message_type = "video"
        else:
            message_type = "text"
        self.record_communication(
            communication_id=communication_id,
            incident_id=f"draft:{draft.draft_id}",
            sender_role="tenant",
            sender_id=draft.tenant_id,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel="telegram",
            direction="inbound",
            message_type=message_type,
            text=clean_text or "Tenant added media to the draft report.",
            media_ids=[asset.asset_id for asset in stored_media],
            provider_message_id=provider_message_id,
            delivery_status="received",
        )
        return draft

    def undo_telegram_draft(self, draft_id: str, telegram_chat_id: str) -> TelegramDraft:
        draft = self.repository.get_draft(draft_id)
        if not draft or draft.telegram_chat_id != telegram_chat_id:
            raise ValueError("Telegram draft is unavailable")
        if draft.submitted_incident_id:
            raise ValueError("Telegram draft was already submitted")
        if self._draft_now() >= draft.expires_at:
            self.repository.delete_draft(draft_id)
            raise ValueError("Telegram draft has expired; send /report to start again")
        if not draft.items:
            return draft
        item = draft.items.pop()
        draft.item_keys = [key for key in draft.item_keys if key != item.item_key]
        draft.communication_ids = [
            value for value in draft.communication_ids if value != item.communication_id
        ]
        self.repository.delete_communication(item.communication_id)
        if item.text and draft.text_parts and draft.text_parts[-1] == item.text:
            draft.text_parts.pop()
        if item.media_id:
            draft.media = [asset for asset in draft.media if asset.asset_id != item.media_id]
        draft.revision += 1
        draft.updated_at = self._draft_now()
        self.repository.save_draft(draft)
        return draft

    def clear_telegram_draft(self, draft_id: str, telegram_chat_id: str) -> TelegramDraft:
        draft = self.repository.get_draft(draft_id)
        if not draft or draft.telegram_chat_id != telegram_chat_id:
            raise ValueError("Telegram draft is unavailable")
        if draft.submitted_incident_id:
            raise ValueError("Telegram draft was already submitted")
        for communication_id in draft.communication_ids:
            self.repository.delete_communication(communication_id)
        draft.text_parts.clear()
        draft.media.clear()
        draft.items.clear()
        draft.item_keys.clear()
        draft.communication_ids.clear()
        draft.revision += 1
        draft.updated_at = self._draft_now()
        self.repository.save_draft(draft)
        return draft

    def cancel_telegram_draft(self, draft_id: str, telegram_chat_id: str) -> bool:
        draft = self.repository.get_draft(draft_id)
        if not draft or draft.telegram_chat_id != telegram_chat_id:
            return False
        if draft.submitted_incident_id:
            return False
        self.repository.delete_draft(draft_id)
        return True

    def submit_telegram_draft(
        self, draft_id: str, telegram_chat_id: str, provider_message_id: str | None = None
    ) -> Incident:
        draft = self.repository.get_draft(draft_id)
        if not draft or draft.telegram_chat_id != telegram_chat_id:
            raise ValueError("Telegram draft is unavailable")
        if draft.submitted_incident_id:
            return self._get(draft.submitted_incident_id)
        if self._draft_now() >= draft.expires_at:
            self.repository.delete_draft(draft_id)
            raise ValueError("Telegram draft has expired; send /report to start again")
        if not draft.text_parts and not draft.media:
            raise ValueError("Add a description, photo, or voice note before submitting")
        media = []
        for asset in draft.media:
            stored = self.media_store.get(asset.asset_id)
            if not stored:
                raise ValueError("A draft media item is no longer available")
            media.append(stored)
        incident = self.submit_report(
            ReportInput(
                property_id=draft.property_id,
                tenant_id=draft.tenant_id,
                report_text=(
                    "\n".join(item.text for item in draft.items if item.text)
                    or "\n".join(draft.text_parts)
                    or "Tenant submitted a multimodal plumbing report."
                ),
                media=media,
                idempotency_key=f"telegram-draft:{draft.draft_id}",
                source_channel="telegram",
                provider_message_id=provider_message_id or draft.draft_id,
            )
        )
        self.repository.move_communications(f"draft:{draft.draft_id}", incident.incident_id)
        draft.submitted_incident_id = incident.incident_id
        draft.updated_at = self._draft_now()
        self.repository.save_draft(draft)
        return incident

    def draft_summary(self, draft: TelegramDraft) -> str:
        text_count = len(draft.text_parts)
        image_count = sum(asset.mime_type.startswith("image/") for asset in draft.media)
        video_count = sum(asset.mime_type.startswith("video/") for asset in draft.media)
        audio_count = sum(asset.mime_type.startswith("audio/") for asset in draft.media)
        excerpts = [part[:140] for part in draft.text_parts[-3:]]
        excerpt_text = "\n".join(f"• {part}" for part in excerpts) or "• (no text yet)"
        remaining = max(0, int((draft.expires_at - self._draft_now()).total_seconds()))
        return (
            "📝 Repair report draft\n\n"
            f"Text messages: {text_count}\n"
            f"Photos: {image_count}\n"
            f"Videos: {video_count}\n"
            f"Voice notes: {audio_count}\n\n"
            f"Expires in about {remaining // 60}m {remaining % 60:02d}s\n"
            "Type below, tap 📎 for photos/videos, or hold 🎙 for voice.\n"
            "Nothing is submitted until you tap Submit report.\n\n"
            f"{excerpt_text}"
        )

    def draft_summary_action_key(self, draft: TelegramDraft) -> str:
        return f"draft-summary:{draft.draft_id}:{draft.revision}"

    def send_draft_message(
        self,
        draft: TelegramDraft,
        text: str,
        action_key: str,
        reply_markup: dict[str, Any] | None = None,
        message_type: ContactMessageType = "system",
    ) -> str:
        return self._send_outbound(
            incident_id=f"draft:{draft.draft_id}",
            recipient_role="tenant",
            logical_recipient_id=draft.tenant_id,
            delivery_recipient_id=draft.telegram_chat_id,
            text=text,
            action_key=action_key,
            message_type=message_type,
            reply_markup=reply_markup,
        )

    def runtime_metadata(self) -> RuntimeMetadata:
        deployment = "cloud_run" if self.settings.k_service else "local_container"
        return RuntimeMetadata(
            environment=self.settings.app_env,
            deployment=deployment,
            facts_provider=self.extractor.provider_name,
            facts_model=self.settings.gemini_model,
            storage_backend=self.repository.provider_name,
            eventing=f"{self.event_bus.provider_name}+{self.tasks.provider_name}",
            messaging_provider=self.notifications.provider_name,
            demo_clock_enabled=not isinstance(self.clock, SystemClock),
            demo_timings_seconds={
                "urgent_vendor_timeout": self.settings.urgent_vendor_timeout_seconds,
                "routine_vendor_timeout": self.settings.routine_vendor_timeout_seconds,
                "human_vendor_timeout": self.settings.human_vendor_timeout_seconds,
                "tenant_confirmation": self.settings.tenant_confirmation_delay_seconds,
                "warranty_recurrence": self.settings.demo_warranty_period_seconds,
            },
        )

    def create_pairing_code(self, target_type: str, target_id: str) -> PairingCodeResponse:
        if target_type not in {"tenant", "vendor"}:
            raise ValueError("pairing target must be tenant or vendor")
        if target_type == "tenant" and target_id not in self.tenants:
            raise ValueError("unknown tenant pairing target")
        if target_type == "vendor" and not any(
            vendor.vendor_id == target_id for vendor in self.vendors
        ):
            raise ValueError("unknown vendor pairing target")
        username = self.settings.telegram_bot_username
        if not username:
            raise ValueError("TELEGRAM_BOT_USERNAME is required to create a deep link")
        # Pairing credentials expire in wall-clock time even when workflow timestamps
        # use the compressed demo clock.
        now = datetime.now(UTC)
        record = PairingCodeRecord(
            code=secrets.token_urlsafe(18),
            target_type=target_type,  # type: ignore[arg-type]
            target_id=target_id,
            created_at=now,
            expires_at=now + timedelta(minutes=15),
        )
        self.repository.create_pairing_code(record)
        return PairingCodeResponse(
            code=record.code,
            deep_link=f"https://t.me/{username}?start={record.code}",
            target_type=record.target_type,
            target_id=record.target_id,
            expires_at=record.expires_at,
        )

    def mark_telegram_delivery_ready(self, telegram_chat_id: str) -> None:
        started_at = datetime.now(UTC)
        tenant = next(
            (candidate for candidate in self.tenants.values() if candidate.telegram_chat_id == telegram_chat_id),
            None,
        )
        if tenant:
            tenant.telegram_started_at = started_at
            tenant.delivery_ready = True
            self.repository.mark_telegram_delivery_ready(
                "tenant", tenant.tenant_id, telegram_chat_id, started_at
            )
        vendor = next(
            (candidate for candidate in self.vendors if candidate.telegram_chat_id == telegram_chat_id),
            None,
        )
        if vendor:
            vendor.telegram_started_at = started_at
            vendor.delivery_ready = True
            self.repository.mark_telegram_delivery_ready(
                "vendor", vendor.vendor_id, telegram_chat_id, started_at
            )
        if tenant or vendor:
            self.started_telegram_chats.add(telegram_chat_id)

    def consume_pairing_code(self, code: str, telegram_chat_id: str) -> PairingCodeRecord:
        record = self.repository.consume_pairing_code(code, telegram_chat_id, datetime.now(UTC))
        if not record:
            raise ValueError("pairing code is invalid, expired, or already used")
        if record.target_type == "tenant":
            target = self.tenants.get(record.target_id)
            if not target:
                raise ValueError("pairing target no longer exists")
            target.telegram_chat_id = telegram_chat_id
        else:
            vendor_target = next(
                (vendor for vendor in self.vendors if vendor.vendor_id == record.target_id), None
            )
            if not vendor_target:
                raise ValueError("pairing target no longer exists")
            vendor_target.telegram_chat_id = telegram_chat_id
        self.repository.bind_telegram_chat(record.target_type, record.target_id, telegram_chat_id)
        self.mark_telegram_delivery_ready(telegram_chat_id)
        return record

    def _save(self, incident: Incident) -> None:
        incident.updated_at = self.clock.now()
        incident.version += 1
        self.repository.save(incident)

    def record_communication(
        self,
        *,
        communication_id: str,
        incident_id: str,
        sender_role: ContactRole,
        sender_id: str,
        recipient_role: ContactRole,
        recipient_id: str,
        channel: str,
        direction: Literal["inbound", "outbound"],
        message_type: ContactMessageType,
        text: str = "",
        media_ids: list[str] | None = None,
        provider_message_id: str | None = None,
        delivery_status: Literal[
            "received", "sent", "delivered", "failed", "simulated", "deduplicated"
        ] = "received",
    ) -> CommunicationRecord:
        record = CommunicationRecord(
            communication_id=communication_id,
            incident_id=incident_id,
            sender_role=sender_role,
            sender_id=sender_id,
            recipient_role=recipient_role,
            recipient_id=recipient_id,
            channel=channel,
            direction=direction,
            message_type=message_type,
            text=_safe_contact_text(text),
            media_ids=list(media_ids or []),
            provider_message_id=_safe_contact_text(provider_message_id, 200) or None,
            delivery_status=delivery_status,
            timestamp=self.clock.now(),
        )
        self.repository.save_communication(record)
        return record

    def _timeline(
        self,
        incident: Incident,
        kind: str,
        rule_id: str | None = None,
        event_id: str | None = None,
        state_from: IncidentStatus | None = None,
        state_to: IncidentStatus | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_metadata = dict(metadata or {})
        if event_id:
            safe_metadata["causation_event_id"] = event_id
        incident.timeline.append(
            TimelineEntry(
                event_id=f"evt_{uuid4().hex}",
                at=self.clock.now(),
                kind=kind,
                rule_id=rule_id,
                state_from=state_from,
                state_to=state_to,
                metadata=safe_metadata,
            )
        )
        entry = incident.timeline[-1]
        self.event_bus.publish(
            entry.event_id,
            incident.incident_id,
            f"audit.{kind}",
            {
                "rule_id": rule_id,
                "state_from": state_from.value if state_from else None,
                "state_to": state_to.value if state_to else None,
            },
        )
        log.info(
            "incident_timeline",
            incident_id=incident.incident_id,
            event_id=entry.event_id,
            kind=kind,
            rule_id=rule_id,
            state_from=state_from.value if state_from else None,
            state_to=state_to.value if state_to else None,
        )

    def _transition(
        self, incident: Incident, target: IncidentStatus, rule_id: str, event_id: str | None = None
    ) -> None:
        transition = validate_transition(incident.status, target, rule_id)
        incident.status = target
        self._timeline(
            incident,
            kind="state_transition",
            rule_id=rule_id,
            event_id=event_id,
            state_from=transition.from_status,
            state_to=transition.to_status,
        )

    def _send_outbound(
        self,
        *,
        incident_id: str,
        recipient_role: Literal["tenant", "vendor"],
        logical_recipient_id: str,
        delivery_recipient_id: str,
        text: str,
        action_key: str,
        message_type: ContactMessageType = "text",
        media_ids: list[str] | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> str:
        scoped_action_key = f"{incident_id}:{action_key}"
        try:
            outcome = self.notifications.send(
                NotificationMessage(
                    incident_id, delivery_recipient_id, text, scoped_action_key, reply_markup
                )
            )
        except Exception:
            self.record_communication(
                communication_id=f"comm:notify:{incident_id}:{action_key}",
                incident_id=incident_id,
                sender_role="agent",
                sender_id="no-more-buckets",
                recipient_role=recipient_role,
                recipient_id=logical_recipient_id,
                channel=self.notifications.provider_name,
                direction="outbound",
                message_type=message_type,
                text=text,
                media_ids=media_ids,
                delivery_status="failed",
            )
            raise
        delivery_status = (
            "deduplicated"
            if outcome.startswith("deduped:")
            else ("sent" if self.notifications.provider_name == "telegram" else "simulated")
        )
        self.record_communication(
            communication_id=f"comm:notify:{incident_id}:{action_key}",
            incident_id=incident_id,
            sender_role="agent",
            sender_id="no-more-buckets",
            recipient_role=recipient_role,
            recipient_id=logical_recipient_id,
            channel=self.notifications.provider_name,
            direction="outbound",
            message_type=message_type,
            text=text,
            media_ids=media_ids,
            provider_message_id=outcome,
            delivery_status=delivery_status,  # type: ignore[arg-type]
        )
        return outcome

    def _notify(
        self,
        incident: Incident,
        text: str,
        action_key: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        tenant = self.tenants.get(incident.tenant_id)
        if self.notifications.provider_name == "telegram" and (
            not tenant or not tenant.telegram_chat_id
        ):
            self._timeline(
                incident,
                "notification_blocked_until_pairing",
                "TELEGRAM_PAIRING_REQUIRED",
                metadata={"recipient_type": "tenant"},
            )
            return
        recipient_id = (
            tenant.telegram_chat_id if tenant and tenant.telegram_chat_id else incident.tenant_id
        )
        if (
            self.notifications.provider_name == "telegram"
            and recipient_id not in self.started_telegram_chats
        ):
            self._timeline(
                incident,
                "notification_blocked_until_start",
                "TELEGRAM_START_REQUIRED",
                metadata={"recipient_type": "tenant"},
            )
            return
        outcome = self._send_outbound(
            incident_id=incident.incident_id,
            recipient_role="tenant",
            logical_recipient_id=incident.tenant_id,
            delivery_recipient_id=recipient_id,
            text=text,
            action_key=action_key,
            reply_markup=reply_markup,
        )
        self._timeline(
            incident, "notification_sent", "NOTIFY_DEDUPED_SAFE", metadata={"outcome": outcome}
        )

    def _notify_vendor(
        self,
        incident: Incident,
        vendor: Vendor,
        text: str,
        action_key: str,
        reply_markup: dict[str, Any] | None = None,
        message_type: ContactMessageType = "text",
    ) -> None:
        recipient_id = vendor.telegram_chat_id or vendor.vendor_id
        if self.notifications.provider_name == "telegram" and (
            not vendor.telegram_chat_id or recipient_id not in self.started_telegram_chats
        ):
            self._timeline(
                incident,
                "vendor_notification_blocked",
                "TELEGRAM_VENDOR_PAIRING_REQUIRED",
                metadata={"vendor_id": vendor.vendor_id},
            )
            return
        outcome = self._send_outbound(
            incident_id=incident.incident_id,
            recipient_role="vendor",
            logical_recipient_id=vendor.vendor_id,
            delivery_recipient_id=recipient_id,
            text=text,
            action_key=action_key,
            message_type=message_type,
            reply_markup=reply_markup,
        )
        self._timeline(
            incident,
            "vendor_notification_sent",
            "PROVIDER_CONTACT_RECORDED",
            metadata={"vendor_id": vendor.vendor_id, "outcome": outcome},
        )

    def submit_report(self, report: ReportInput) -> Incident:
        idempotency_key = (
            report.idempotency_key
            or f"report:{report.property_id}:{report.tenant_id}:{report.report_text}"
        )
        incident_id = f"inc_{uuid4().hex[:12]}"
        existing_id = self.repository.claim_idempotency(idempotency_key, incident_id)
        if existing_id and existing_id != incident_id:
            existing = self.repository.get(existing_id)
            if existing:
                return existing
        config = self.properties.get(report.property_id)
        if not config:
            raise ValueError("unknown property")
        for asset in report.media:
            validate_media_asset(asset)
            asset.storage_uri = self.media_store.put(asset)
        now = self.clock.now()
        incident = Incident(
            incident_id=incident_id,
            property_id=report.property_id,
            tenant_id=report.tenant_id,
            report_text=" ".join(report.report_text.split())[:4000],
            voice_transcript=report.voice_transcript,
            media_ids=[asset.asset_id for asset in report.media],
            created_at=now,
            updated_at=now,
        )
        if report.media and not report.report_text.strip():
            message_type: ContactMessageType = (
                "audio"
                if any(asset.mime_type.startswith("audio/") for asset in report.media)
                else "image"
            )
        else:
            message_type = "text"
        self.record_communication(
            communication_id=f"comm:report:{sha256(idempotency_key.encode()).hexdigest()[:24]}",
            incident_id=incident.incident_id,
            sender_role="tenant",
            sender_id=report.tenant_id,
            recipient_role="agent",
            recipient_id="no-more-buckets",
            channel=report.source_channel,
            direction="inbound",
            message_type=message_type,
            text=report.report_text or report.voice_transcript or "Tenant submitted media.",
            media_ids=[asset.asset_id for asset in report.media],
            provider_message_id=report.provider_message_id,
            delivery_status="received",
        )
        self._timeline(incident, "report_received", "REPORT_ACCEPTED")
        assessor = getattr(self.extractor, "assess", None)
        if callable(assessor):
            assessment = assessor(report.report_text, report.voice_transcript, report.media)
        else:
            facts = self.extractor.extract(
                report.report_text, report.voice_transcript, report.media
            )
            assessment = ReportAssessment(
                voice_transcript=report.voice_transcript,
                facts=facts,
                confidence=facts.source_confidence,
            )
        incident.report_assessment = assessment
        incident.voice_transcript = assessment.voice_transcript or report.voice_transcript
        incident.facts = assessment.facts
        self._transition(incident, IncidentStatus.TRIAGED, "FACTS_SCHEMA_VALIDATED")
        safety = evaluate_safety(incident.facts)
        if safety.escalate:
            self._transition(incident, IncidentStatus.ESCALATED, safety.rule_id)
            self._timeline(
                incident, "approval_required", safety.rule_id, metadata={"reason": safety.reason}
            )
            self._notify(
                incident,
                "We need a property manager to review a safety exception before work begins.",
                "safety-escalation",
            )
            self._save(incident)
            return incident
        incident.containment_instructions = property_specific_containment(config, incident.facts)
        self._transition(
            incident, IncidentStatus.CONTAINED, "CONTAINMENT_INSTRUCTIONS_PROPERTY_SPECIFIC"
        )
        self._notify(incident, incident.containment_instructions, "containment-instructions")
        incident.work_order = build_bounded_work_order(incident, config, incident.facts)
        incident.work_order_id = incident.work_order.work_order_id
        self._timeline(
            incident,
            "work_order_bounded",
            "SPENDING_LIMIT_ENFORCED",
            metadata={
                "currency": incident.work_order.currency,
                "limit": incident.work_order.spending_limit,
                "estimate": incident.work_order.estimated_cost,
            },
        )
        self._save(incident)
        if requires_spending_approval(incident.work_order):
            assert incident.work_order.estimated_cost is not None
            incident.approval = ApprovalRequest(
                approval_id=f"apr_{uuid4().hex[:12]}",
                incident_id=incident.incident_id,
                reason="estimated work exceeds property spending limit",
                requested_amount=incident.work_order.estimated_cost,
                limit=incident.work_order.spending_limit,
                created_at=self.clock.now(),
            )
            self._transition(incident, IncidentStatus.ESCALATED, "SPENDING_LIMIT_APPROVAL_REQUIRED")
            self._notify(
                incident,
                "This repair exceeds the autonomous spending limit and needs approval.",
                "spending-approval",
            )
            self._save(incident)
            return incident
        self._transition(incident, IncidentStatus.DISPATCHING, "DISPATCH_POLICY_ALLOWED")
        self._save(incident)
        self._dispatch_next(incident, trigger_event_id=f"dispatch:{incident.incident_id}")
        return self.repository.get(incident.incident_id) or incident

    def _task_id(self, kind: str, incident_id: str, suffix: str = "") -> str:
        digest = sha256(f"{kind}:{incident_id}:{suffix}".encode()).hexdigest()[:24]
        return f"{kind}-{digest}"

    def _vendor_timeout_seconds(self, incident: Incident, vendor: Vendor) -> int:
        is_human = getattr(self.vendors_adapter, "is_human_vendor", None)
        if callable(is_human) and is_human(vendor):
            return self.settings.human_vendor_timeout_seconds
        if incident.facts and incident.facts.severity.value in {"high", "critical"}:
            return self.settings.urgent_vendor_timeout_seconds
        return self.settings.routine_vendor_timeout_seconds

    def _accept_vendor(self, incident: Incident, vendor: Vendor, event_id: str) -> None:
        assert incident.work_order is not None
        incident.assigned_vendor_id = vendor.vendor_id
        incident.work_order.vendor_id = vendor.vendor_id
        incident.work_order.status = "dispatched"
        if incident.status != IncidentStatus.SCHEDULED:
            self._transition(
                incident, IncidentStatus.SCHEDULED, "VENDOR_ACCEPTED_ELIGIBLE", event_id
            )
        self._notify(
            incident,
            f"{vendor.name} accepted your repair request. They’re confirming the arrival time now.",
            f"vendor-accepted:{vendor.vendor_id}",
        )

    def _dispatch_next(self, incident: Incident, trigger_event_id: str) -> None:
        if incident.status not in {
            IncidentStatus.DISPATCHING,
            IncidentStatus.REOPENED,
            IncidentStatus.CONTAINED,
        }:
            return
        if any(attempt.outcome == "pending" for attempt in incident.vendor_attempts):
            return
        config = self.properties[incident.property_id]
        attempted = {attempt.vendor_id for attempt in incident.vendor_attempts}
        candidates = [
            vendor
            for vendor in rank_eligible_vendors(self.vendors, config)
            if vendor.vendor_id not in attempted
        ]
        if not candidates:
            if incident.status != IncidentStatus.ESCALATED:
                self._transition(incident, IncidentStatus.ESCALATED, "VENDOR_POOL_EXHAUSTED")
            self._timeline(incident, "approval_required", "VENDOR_POOL_EXHAUSTED")
            self._notify(
                incident,
                "No eligible vendor accepted this bounded work order; a manager must review it.",
                "vendor-pool-exhausted",
            )
            self._save(incident)
            return
        vendor = candidates[0]
        if vendor.telegram_chat_id:
            session = self.create_vendor_session(incident, vendor)
            if incident.work_order:
                incident.work_order.vendor_session_id = session.session_id
        assert incident.work_order is not None
        vendor_channel = getattr(self.vendors_adapter, "provider_name", "vendor_adapter")
        try:
            result = self.vendors_adapter.dispatch(
                incident.work_order, vendor, f"dispatch:{incident.incident_id}:{vendor.vendor_id}"
            )
        except Exception:
            self.record_communication(
                communication_id=f"comm:dispatch:{incident.incident_id}:{vendor.vendor_id}",
                incident_id=incident.incident_id,
                sender_role="agent",
                sender_id="no-more-buckets",
                recipient_role="vendor",
                recipient_id=vendor.vendor_id,
                channel=vendor_channel,
                direction="outbound",
                message_type="button",
                text="Vendor dispatch provider failed; retry scheduled.",
                provider_message_id=None,
                delivery_status="failed",
            )
            retry_count = sum(
                1
                for entry in incident.timeline
                if entry.kind == "vendor_provider_retry_scheduled"
                and entry.metadata.get("vendor_id") == vendor.vendor_id
            )
            if retry_count >= 3:
                self._transition(
                    incident, IncidentStatus.ESCALATED, "VENDOR_PROVIDER_RETRY_EXHAUSTED"
                )
                self._timeline(
                    incident,
                    "approval_required",
                    "VENDOR_PROVIDER_RETRY_EXHAUSTED",
                    metadata={"vendor_id": vendor.vendor_id},
                )
                self._save(incident)
                return
            retry_task_id = self._task_id("vendor-retry", incident.incident_id, vendor.vendor_id)
            self._enqueue_task(
                retry_task_id,
                incident.incident_id,
                "vendor_retry",
                {"vendor_id": vendor.vendor_id, "retry_count": retry_count + 1},
                2 if self.settings.demo_mode else 30,
            )
            self._timeline(
                incident,
                "vendor_provider_retry_scheduled",
                "TRANSIENT_PROVIDER_FAILURE_RETRY",
                metadata={"vendor_id": vendor.vendor_id, "task_id": retry_task_id},
            )
            self._save(incident)
            return
        dispatch_status = (
            "deduplicated"
            if result.provider_event_id.startswith("deduped:")
            else (
                "sent"
                if vendor_channel in {"telegram_vendor_dispatch", "telegram_demo_vendor"}
                and not result.provider_event_id.startswith("demo-")
                else "simulated"
            )
        )
        self.record_communication(
            communication_id=f"comm:dispatch:{incident.incident_id}:{vendor.vendor_id}",
            incident_id=incident.incident_id,
            sender_role="agent",
            sender_id="no-more-buckets",
            recipient_role="vendor",
            recipient_id=vendor.vendor_id,
            channel=vendor_channel,
            direction="outbound",
            message_type="button",
            text=result.text or f"Bounded work order dispatched to {vendor.name}.",
            provider_message_id=result.provider_event_id,
            delivery_status=dispatch_status,  # type: ignore[arg-type]
        )
        outcome = result.outcome
        attempt_outcome = {"accept": "accepted", "decline": "declined", "timeout": "timed_out"}.get(
            outcome, outcome
        )
        timeout_seconds = self._vendor_timeout_seconds(incident, vendor) if outcome == "pending" else 0
        deadline = self.clock.now() + timedelta(seconds=timeout_seconds) if outcome == "pending" else None
        attempt = VendorAttempt(
            vendor_id=vendor.vendor_id,
            outcome=attempt_outcome,  # type: ignore[arg-type]
            attempt_id=f"attempt_{uuid4().hex[:12]}",
            event_id=result.provider_event_id,
            at=self.clock.now(),
            deadline_at=deadline,
        )
        incident.vendor_attempts.append(attempt)
        self._timeline(
            incident,
            "vendor_dispatch_outcome",
            "VENDOR_FALLBACK_ON_FAILURE",
            event_id=result.provider_event_id,
            metadata={"vendor_id": vendor.vendor_id, "outcome": outcome},
        )
        if outcome == "accept":
            self._accept_vendor(incident, vendor, result.provider_event_id)
            self._save(incident)
            return
        if outcome == "pending":
            task_id = self._task_id("vendor-timeout", incident.incident_id, vendor.vendor_id)
            self._enqueue_task(
                task_id,
                incident.incident_id,
                "vendor_timeout",
                {"vendor_id": vendor.vendor_id},
                timeout_seconds,
            )
            self._timeline(
                incident,
                "vendor_timeout_scheduled",
                "VENDOR_RESPONSE_SLA",
                metadata={
                    "vendor_id": vendor.vendor_id,
                    "task_id": task_id,
                    "timeout_seconds": timeout_seconds,
                    "deadline_at": deadline.isoformat() if deadline else None,
                },
            )
            self._save(incident)
            return
        self._notify(
            incident,
            f"{vendor.name} could not accept this repair; contacting the next eligible vendor.",
            f"vendor-failed:{vendor.vendor_id}:{outcome}",
        )
        self._save(incident)
        self._dispatch_next(
            incident, trigger_event_id=f"fallback:{incident.incident_id}:{vendor.vendor_id}"
        )

    def _get(self, incident_id: str) -> Incident:
        incident = self.repository.get(incident_id)
        if not incident:
            raise IncidentNotFound(incident_id)
        return incident

    def get_vendor_session(self, session_id: str) -> VendorSession | None:
        return self.repository.get_vendor_session(session_id)

    def create_vendor_session(self, incident: Incident, vendor: Vendor) -> VendorSession:
        existing = self.repository.find_vendor_session(
            vendor.telegram_chat_id or "", vendor.vendor_id, incident.incident_id
        )
        if existing:
            return existing
        now = self.clock.now()
        session = VendorSession(
            incident_id=incident.incident_id,
            vendor_id=vendor.vendor_id,
            telegram_chat_id=vendor.telegram_chat_id or "",
            created_at=now,
            updated_at=now,
        )
        self.repository.save_vendor_session(session)
        return session

    def _save_vendor_session(self, session: VendorSession) -> VendorSession:
        session.revision += 1
        session.updated_at = self.clock.now()
        self.repository.save_vendor_session(session)
        return session

    def _terminalize_vendor_session(self, incident_id: str, vendor_id: str, stage: str) -> None:
        vendor = next((candidate for candidate in self.vendors if candidate.vendor_id == vendor_id), None)
        if not vendor:
            return
        session = self.repository.find_vendor_session(vendor.telegram_chat_id or "", vendor_id, incident_id)
        if session and session.stage not in {"DECLINED", "TIMED_OUT", "RELEASED", "COMPLETED"}:
            session.stage = stage  # type: ignore[assignment]
            session.cancelled = True
            self._save_vendor_session(session)

    def _session_context(self, session: VendorSession) -> tuple[Incident, Vendor]:
        incident = self._get(session.incident_id)
        vendor = next((v for v in self.vendors if v.vendor_id == session.vendor_id), None)
        if not vendor or vendor.telegram_chat_id != session.telegram_chat_id:
            raise ValueError("vendor session is not authorized")
        return incident, vendor

    @staticmethod
    def _force_reply(placeholder: str) -> dict[str, Any]:
        return {"force_reply": True, "input_field_placeholder": placeholder}

    def accept_vendor_session(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage != "OFFERED" or incident.assigned_vendor_id != vendor.vendor_id:
            raise ValueError("This offer is no longer current. Use /status for the current step.")
        session.stage = "AWAITING_PRICE"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            "✅ Job reserved for you\n\nStep 1 of 2 — Quote\n\n"
            "Reply to this message with the estimated total in SGD.\n\n"
            "Examples:\n220\nS$220\nPRICE 220.50\n\nNothing is submitted yet.",
            f"vendor-session-price:{session.session_id}",
            self._force_reply("SGD amount, e.g. 220.00"),
        )
        return session

    def vendor_price(self, session: VendorSession, amount: float) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage not in {"AWAITING_PRICE", "CONFIRMING_PRICE"}:
            raise ValueError("Price is not the current step. Use /status for the current step.")
        session.draft_price = round(amount, 2)
        session.stage = "CONFIRMING_PRICE"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            f"Confirm quote\n\nEstimated total: S${session.draft_price:.2f}\n\nThis has not been submitted.",
            f"vendor-session-price-review:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[
                {"text": f"Confirm S${session.draft_price:.2f}", "callback_data": f"vs:{session.session_id}:pc"},
                {"text": "Edit price", "callback_data": f"vs:{session.session_id}:pe"},
            ], [{"text": "Cancel intake", "callback_data": f"vs:{session.session_id}:cx"}] ]},
        )
        return session

    def confirm_vendor_price(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage != "CONFIRMING_PRICE" or session.draft_price is None:
            raise ValueError("There is no price awaiting confirmation.")
        session.price_confirmed = True
        session.stage = "AWAITING_ETA"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            "Step 2 of 2 — Arrival ETA\n\nReply with the number of minutes until arrival.\n\n"
            "Examples:\n20\nETA 20\n20 minutes\n\nNothing is submitted yet.",
            f"vendor-session-eta:{session.session_id}", self._force_reply("Minutes until arrival, e.g. 20"),
        )
        return session

    def vendor_eta(self, session: VendorSession, minutes: int) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage not in {"AWAITING_ETA", "CONFIRMING_ETA"} or not session.price_confirmed:
            raise ValueError("ETA is not the current step. Use /status for the current step.")
        session.draft_eta = minutes
        session.stage = "CONFIRMING_ETA"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            f"Confirm arrival ETA\n\nArrival: {minutes} minutes\n\nThis has not been submitted.",
            f"vendor-session-eta-review:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[
                {"text": f"Confirm {minutes} minutes", "callback_data": f"vs:{session.session_id}:ec"},
                {"text": "Edit ETA", "callback_data": f"vs:{session.session_id}:ee"},
            ], [{"text": "Back to price", "callback_data": f"vs:{session.session_id}:eb"}] ]},
        )
        return session

    def legacy_vendor_input(self, session: VendorSession, amount: float, minutes: int) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage not in {"AWAITING_PRICE", "AWAITING_ETA", "CONFIRMING_PRICE", "CONFIRMING_ETA"}:
            raise ValueError("This combined response is not the current step. Use /status.")
        session.draft_price = amount
        session.draft_eta = minutes
        session.price_confirmed = False
        session.eta_confirmed = False
        session.stage = "REVIEW"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            f"Review your response\n\nQuote: S${amount:.2f}\nArrival ETA: {minutes} minutes\nWork order: {incident.work_order_id}\n\nNothing has been submitted yet.\n\nThe legacy combined format was saved as a draft. Explicitly confirm the quote and ETA before submitting.",
            f"vendor-legacy-review:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[{"text": "Edit price", "callback_data": f"vs:{session.session_id}:pe"}, {"text": "Edit ETA", "callback_data": f"vs:{session.session_id}:ee"}], [{"text": "Cancel intake", "callback_data": f"vs:{session.session_id}:cx"}]]},
        )
        return session

    def confirm_vendor_eta(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage != "CONFIRMING_ETA" or session.draft_eta is None:
            raise ValueError("There is no ETA awaiting confirmation.")
        session.eta_confirmed = True
        session.stage = "REVIEW"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident, vendor,
            f"Review your response\n\nQuote: S${session.draft_price:.2f}\n"
            f"Arrival ETA: {session.draft_eta} minutes\nWork order: {incident.work_order_id}\n"
            f"Property: {incident.work_order.property_name if incident.work_order else 'reported unit'}\n\nNothing has been submitted yet.",
            f"vendor-session-review:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[{"text": "Submit quote and ETA", "callback_data": f"vs:{session.session_id}:su"}],
             [{"text": "Edit price", "callback_data": f"vs:{session.session_id}:pe"}, {"text": "Edit ETA", "callback_data": f"vs:{session.session_id}:ee"}],
             [{"text": "Reset intake", "callback_data": f"vs:{session.session_id}:cx"}]]},
        )
        return session

    def submit_vendor_quote(self, session: VendorSession, event_id: str) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage != "REVIEW" or not session.price_confirmed or not session.eta_confirmed:
            raise ValueError("Complete and review both fields before submitting.")
        assert session.draft_price is not None and session.draft_eta is not None
        self.process_action(incident.incident_id, ActionRequest(
            action="vendor_quote", event_id=f"{event_id}:quote",
            payload={"vendor_id": vendor.vendor_id, "amount": session.draft_price},
        ))
        self.process_action(incident.incident_id, ActionRequest(
            action="eta", event_id=f"{event_id}:eta",
            payload={"vendor_id": vendor.vendor_id, "eta_minutes": session.draft_eta},
        ))
        session.final_price = session.draft_price
        session.final_price_confirmed = True
        session.submitted = True
        session.stage = "SUBMITTED"
        self._save_vendor_session(session)
        current = self._get(incident.incident_id)
        if current.approval and current.approval.status == "pending":
            self._notify_vendor(
                current, vendor,
                f"⚠️ Manager approval required\n\nYour S${session.draft_price:.2f} quote exceeds the S$250 autonomous limit.\n\nDo not travel or begin work until approval is received.",
                f"vendor-approval-pending:{session.session_id}",
            )
        else:
            self._notify_vendor(
                current, vendor,
                f"✅ Quote and ETA submitted\n\nQuote: S${session.draft_price:.2f}\nArrival ETA: {session.draft_eta} minutes\n\nTravel to the property. When you arrive, tap Start job.",
                f"vendor-submitted:{session.session_id}",
                {"inline_keyboard": [[{"text": "Start job", "callback_data": f"vs:{session.session_id}:st"}]]},
            )
        return session

    def cancel_vendor_session(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.submitted and session.stage in {"AWAITING_PHOTO", "AWAITING_SCOPE", "COMPLETION_REVIEW", "CONFIRMING_FINAL_PRICE"}:
            session.completion_photo_ids = []
            session.completion_scope = None
            session.final_price = session.draft_price
            session.final_price_confirmed = False
            session.cancelled = False
            session.stage = "SUBMITTED"
            self._save_vendor_session(session)
            return session
        if session.submitted:
            raise ValueError("An accepted job cannot be cancelled with /cancel.")
        if session.stage == "OFFERED":
            raise ValueError("This offer cannot be cancelled. Tap Accept job or Decline job.")
        if incident.assigned_vendor_id == vendor.vendor_id and incident.status.value == "SCHEDULED":
            session.draft_price = None
            session.draft_eta = None
            session.price_confirmed = False
            session.eta_confirmed = False
            session.final_price = None
            session.final_price_confirmed = False
            session.cancelled = False
            session.stage = "AWAITING_PRICE"
            self._save_vendor_session(session)
            return session
        session.cancelled = True
        session.stage = "CANCELLED"
        self._save_vendor_session(session)
        return session

    def start_vendor_job(self, session: VendorSession, event_id: str) -> Incident:
        incident, vendor = self._session_context(session)
        if (session.stage != "SUBMITTED" or not session.submitted or not session.price_confirmed
                or not session.eta_confirmed or incident.assigned_vendor_id != vendor.vendor_id
                or incident.approval and incident.approval.status == "pending"):
            self._timeline(incident, "work_started_blocked", "VENDOR_SESSION_PRECONDITIONS", event_id=event_id)
            self._save(incident)
            raise ValueError("Start job is not available yet. Use /status for the current state.")
        current = self.process_action(incident.incident_id, ActionRequest(
            action="work_started", event_id=event_id, payload={"vendor_id": vendor.vendor_id}
        ))
        session.stage = "SUBMITTED"
        self._save_vendor_session(session)
        return current

    def prepare_completion(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if incident.status.value != "IN_PROGRESS" or not session.submitted:
            raise ValueError("Completion is available after Start job.")
        self._notify_vendor(
            incident, vendor,
            "🛠 Job started\n\nComplete the bounded repair within the confirmed scope and price.\n\nWhen the repair is finished, tap Prepare completion. You will need:\n\n• One clear after-photo\n• A 10–500 character work summary\n• Confirmation of the final price",
            f"completion-ready:{session.session_id}", {"inline_keyboard": [[{"text": "Prepare completion", "callback_data": f"vs:{session.session_id}:pr"}]]},
        )
        return session

    def begin_completion(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if incident.status.value != "IN_PROGRESS" or not session.submitted or session.stage != "SUBMITTED":
            raise ValueError("Completion is available after Start job.")
        session.stage = "AWAITING_PHOTO"
        self._save_vendor_session(session)
        self._notify_vendor(incident, vendor, "📷 Completion step 1 of 2\n\nReply to this message with one clear after-photo showing the repaired area and surrounding surface.", f"completion-photo-prompt:{session.session_id}", self._force_reply("Attach one clear after-photo"))
        return session

    def completion_photo(self, session: VendorSession, media: list[Any]) -> VendorSession:
        incident, vendor = self._session_context(session)
        photo = next((asset for asset in media if asset.mime_type.startswith("image/")), None)
        if not photo:
            self._notify_vendor(incident, vendor, "❌ Please attach one clear after-photo. Text, voice, and video alone cannot replace the required photo.", f"completion-photo-retry:{session.session_id}", self._force_reply("Attach one clear after-photo"))
            return session
        validate_media_asset(photo)
        photo.storage_uri = self.media_store.put(photo)
        if photo.asset_id not in incident.media_ids:
            incident.media_ids.append(photo.asset_id)
        self._save(incident)
        session.completion_photo_ids = [photo.asset_id]
        session.stage = "AWAITING_SCOPE"
        self._save_vendor_session(session)
        self._notify_vendor(incident, vendor, "📝 Completion step 2 of 2\n\nBriefly describe what you repaired and any part replaced.\n\nExample:\nReplaced the failed sink seal and tested the joint with the water running.", f"completion-scope-prompt:{session.session_id}", self._force_reply("Describe work performed (10–500 characters)"))
        return session

    def completion_scope(self, session: VendorSession, scope: str) -> VendorSession:
        incident, vendor = self._session_context(session)
        meaningful = " ".join(scope.split())
        if session.stage != "AWAITING_SCOPE" or not 10 <= len(meaningful) <= 500:
            self._notify_vendor(incident, vendor, "❌ Please describe the work performed in 10–500 meaningful characters.", f"completion-scope-retry:{session.session_id}", self._force_reply("Describe work performed (10–500 characters)"))
            return session
        session.completion_scope = meaningful
        session.final_price = session.final_price or session.draft_price
        session.final_price_confirmed = False
        session.stage = "CONFIRMING_FINAL_PRICE"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident,
            vendor,
            f"Confirm final price\n\nAfter-photo: Attached\nWork performed: {meaningful}\nFinal price: S${session.final_price:.2f}\n\nConfirm this price before Submit completion.",
            f"completion-price-confirmation:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[{"text": f"Confirm S${session.final_price:.2f}", "callback_data": f"vs:{session.session_id}:fp"}, {"text": "Change final price", "callback_data": f"vs:{session.session_id}:cf"}], [{"text": "Cancel completion draft", "callback_data": f"vs:{session.session_id}:cx"}]]},
        )
        return session

    def submit_completion(self, session: VendorSession, event_id: str) -> Incident:
        incident, vendor = self._session_context(session)
        if session.stage != "COMPLETION_REVIEW" or not session.completion_photo_ids or not session.completion_scope or not session.final_price_confirmed:
            raise ValueError("Completion requires a photo, work summary, and confirmed final price.")
        if incident.approval and incident.approval.status == "pending":
            raise ValueError("Manager approval is required before completion can be submitted.")
        photo = self.media_store.get(session.completion_photo_ids[0])
        if not photo or session.final_price is None:
            raise ValueError("Completion evidence is unavailable; please replace the photo.")
        invoice = Invoice(invoice_id=f"invoice_{incident.incident_id}_{event_id}", vendor_id=vendor.vendor_id, currency="SGD", total=session.final_price, line_items=[InvoiceLineItem(description=f"repair work: {session.completion_scope}", quantity=1, unit_price=session.final_price)])
        current = self.process_action(incident.incident_id, ActionRequest(action="completion", event_id=event_id, payload={"photo": photo.model_dump(), "invoice": invoice.model_dump()}))
        if (
            current.status == IncidentStatus.PROVISIONALLY_RESOLVED
            and current.last_evidence is not None
            and current.last_evidence.passed
        ):
            session.submitted = True
            session.stage = "COMPLETED"
            self._save_vendor_session(session)
            self._notify_vendor(current, vendor, "✅ Completion submitted\n\nThe evidence passed initial validation and has been sent to the tenant.\n\nWaiting for the tenant to confirm the repair is dry.", f"completion-submitted:{session.session_id}")
        else:
            reasons = current.last_evidence.blocking_reasons if current.last_evidence else ["completion evidence could not be validated"]
            safe_reasons = "\n".join(f"• {reason}" for reason in reasons)
            session.submitted = False
            session.completion_photo_ids = []
            session.final_price_confirmed = False
            if current.status == IncidentStatus.ESCALATED:
                session.stage = "AWAITING_FINAL_APPROVAL" if current.approval and current.approval.status == "pending" else "SUBMITTED"
                next_action = "Manager review is required before another completion submission can succeed."
            else:
                session.stage = "AWAITING_PHOTO"
                next_action = "Attach one corrected after-photo to retry completion evidence. Your work summary and final price are still saved."
            self._save_vendor_session(session)
            self._notify_vendor(current, vendor, f"❌ Completion was not accepted.\n\nBlocking reason(s):\n{safe_reasons}\n\n{next_action}", f"completion-blocked:{session.session_id}")
        return current

    def change_final_price(self, session: VendorSession, amount: float) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage not in {"COMPLETION_REVIEW", "CONFIRMING_FINAL_PRICE"}:
            raise ValueError("Final price can only be changed from the completion review.")
        session.final_price = round(amount, 2)
        session.final_price_confirmed = False
        session.stage = "CONFIRMING_FINAL_PRICE"
        self._save_vendor_session(session)
        self._notify_vendor(incident, vendor, f"Confirm final price\n\nFinal price: S${amount:.2f}\n\nThis has not been submitted.", f"completion-price-review:{session.session_id}:{session.revision}", {"inline_keyboard": [[{"text": f"Confirm S${amount:.2f}", "callback_data": f"vs:{session.session_id}:fp"}, {"text": "Edit final price", "callback_data": f"vs:{session.session_id}:cf"}]]})
        return session

    def begin_final_price_edit(self, session: VendorSession) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage not in {"COMPLETION_REVIEW", "CONFIRMING_FINAL_PRICE"}:
            raise ValueError("Final price can only be changed from the completion review.")
        session.stage = "CONFIRMING_FINAL_PRICE"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident,
            vendor,
            "Changing the final price requires a new confirmed SGD amount. Send it now.",
            f"completion-price-edit:{session.session_id}:{session.revision}",
            self._force_reply("Final SGD price, e.g. 220.00"),
        )
        return session

    def confirm_final_price(self, session: VendorSession, event_id: str) -> VendorSession:
        incident, vendor = self._session_context(session)
        if session.stage != "CONFIRMING_FINAL_PRICE" or session.final_price is None:
            raise ValueError("There is no final price awaiting confirmation.")
        if incident.work_order and session.final_price > incident.work_order.authorized_amount:
            self.process_action(incident.incident_id, ActionRequest(action="vendor_quote", event_id=f"{event_id}:approval", payload={"vendor_id": vendor.vendor_id, "amount": session.final_price}))
            current = self._get(incident.incident_id)
            if current.approval and current.approval.status == "pending":
                session.stage = "AWAITING_FINAL_APPROVAL"
                self._save_vendor_session(session)
                self._notify_vendor(current, vendor, "⚠️ Final price approval is pending. Your completion draft is saved; Submit completion is unavailable until a manager approves it.", f"completion-approval-pending:{session.session_id}")
                return session
        session.final_price_confirmed = True
        session.stage = "COMPLETION_REVIEW"
        self._save_vendor_session(session)
        self._notify_vendor(
            incident,
            vendor,
            f"Review completion\n\nAfter-photo: Attached\nWork performed: {session.completion_scope}\nFinal price: S${session.final_price:.2f}\n\nNothing has been submitted yet.",
            f"completion-review-final-price:{session.session_id}:{session.revision}",
            {"inline_keyboard": [[{"text": "Submit completion", "callback_data": f"vs:{session.session_id}:cs"}], [{"text": "Replace photo", "callback_data": f"vs:{session.session_id}:cr"}, {"text": "Edit work summary", "callback_data": f"vs:{session.session_id}:ce"}], [{"text": "Change final price", "callback_data": f"vs:{session.session_id}:cf"}, {"text": "Cancel completion draft", "callback_data": f"vs:{session.session_id}:cx"}]]},
        )
        return session

    def process_action(self, incident_id: str, request: ActionRequest) -> Incident:
        # Validate nested untrusted input before it consumes the idempotency key.
        if request.action == "completion":
            CompletionEvidence.model_validate(request.payload)
        incident = self._get(incident_id)
        action = request.action
        if action != "vendor_timeout" and not self.repository.claim_event(request.event_id):
            return incident
        if action == "vendor_a_late_accept":
            self._timeline(
                incident,
                "late_vendor_acceptance_ignored",
                "ASSIGNED_VENDOR_WINS_RACE",
                event_id=request.event_id,
                metadata={
                    "vendor_id": "vendor-a",
                    "assigned_vendor_id": incident.assigned_vendor_id,
                },
            )
        elif action == "vendor_retry":
            if incident.status == IncidentStatus.DISPATCHING:
                self._dispatch_next(incident, request.event_id)
            else:
                self._timeline(
                    incident,
                    "vendor_retry_ignored",
                    "VENDOR_RETRY_NOT_DISPATCHING",
                    event_id=request.event_id,
                )
        elif action == "vendor_timeout":
            vendor_id = str(request.payload.get("vendor_id", ""))
            pending_attempt = next(
                (
                    attempt
                    for attempt in incident.vendor_attempts
                    if attempt.vendor_id == vendor_id and attempt.outcome == "pending"
                ),
                None,
            )
            if (
                pending_attempt
                and incident.status == IncidentStatus.DISPATCHING
                and pending_attempt.deadline_at
                and self.clock.now() < pending_attempt.deadline_at
            ):
                remaining_seconds = max(
                    1, int((pending_attempt.deadline_at - self.clock.now()).total_seconds())
                )
                replacement_task_id = self._task_id(
                    "vendor-timeout-reschedule",
                    incident.incident_id,
                    f"{pending_attempt.attempt_id}:{pending_attempt.deadline_at.isoformat()}:{request.event_id}",
                )
                # Enqueue before claiming the source event. A transient queue failure
                # must leave the original delivery retryable.
                self._enqueue_task(
                    replacement_task_id,
                    incident.incident_id,
                    "vendor_timeout",
                    {"vendor_id": vendor_id, "attempt_id": pending_attempt.attempt_id},
                    remaining_seconds,
                )
                if not self.repository.claim_event(request.event_id):
                    return incident
                self._timeline(
                    incident,
                    "vendor_timeout_ignored",
                    "VENDOR_TIMEOUT_BEFORE_DEADLINE",
                    event_id=request.event_id,
                    metadata={
                        "vendor_id": vendor_id,
                        "replacement_task_id": replacement_task_id,
                        "remaining_seconds": remaining_seconds,
                        "original_deadline_at": pending_attempt.deadline_at.isoformat(),
                    },
                )
                self._save(incident)
                return incident
            if not self.repository.claim_event(request.event_id):
                return incident
            if incident.assigned_vendor_id and incident.assigned_vendor_id != vendor_id:
                self._timeline(
                    incident,
                    "late_vendor_timeout_ignored",
                    "ASSIGNED_VENDOR_WINS_RACE",
                    event_id=request.event_id,
                    metadata={
                        "vendor_id": vendor_id,
                        "assigned_vendor_id": incident.assigned_vendor_id,
                    },
                )
            elif pending_attempt and incident.status == IncidentStatus.DISPATCHING:
                pending_attempt.outcome = "timed_out"
                self._terminalize_vendor_session(incident.incident_id, vendor_id, "TIMED_OUT")
                self._timeline(
                    incident,
                    "vendor_timeout_received",
                    "VENDOR_FALLBACK_ON_FAILURE",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id},
                )
                self.record_communication(
                    communication_id=f"comm:fallback:{incident.incident_id}:{vendor_id}",
                    incident_id=incident.incident_id,
                    sender_role="scheduler",
                    sender_id="no-more-buckets",
                    recipient_role="agent",
                    recipient_id="no-more-buckets",
                    channel="workflow",
                    direction="inbound",
                    message_type="system",
                    text="Vendor A did not respond; automatically contacting Vendor B.",
                    provider_message_id=request.event_id,
                    delivery_status="simulated",
                )
                self._notify(
                    incident,
                    "Vendor A did not respond; automatically contacting Vendor B.",
                    "vendor-timeout-fallback",
                )
                self._dispatch_next(incident, request.event_id)
            else:
                self._timeline(
                    incident,
                    "vendor_timeout_ignored",
                    "VENDOR_TIMEOUT_NOT_PENDING",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id},
                )
        elif action == "vendor_response":
            vendor_id = str(request.payload.get("vendor_id", ""))
            outcome = str(request.payload.get("outcome", ""))
            pending_attempt = next(
                (
                    attempt
                    for attempt in incident.vendor_attempts
                    if attempt.vendor_id == vendor_id and attempt.outcome == "pending"
                ),
                None,
            )
            vendor = next(
                (candidate for candidate in self.vendors if candidate.vendor_id == vendor_id), None
            )
            if not pending_attempt or not vendor or incident.status != IncidentStatus.DISPATCHING:
                self._timeline(
                    incident,
                    "vendor_response_ignored",
                    "VENDOR_RESPONSE_NOT_CURRENT",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id, "outcome": outcome},
                )
            elif outcome == "accept":
                pending_attempt.outcome = "accepted"
                self._timeline(
                    incident,
                    "vendor_dispatch_outcome",
                    "VENDOR_ACCEPTED_ELIGIBLE",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id, "outcome": outcome},
                )
                self._accept_vendor(incident, vendor, request.event_id)
            elif outcome == "decline":
                pending_attempt.outcome = "declined"
                self._terminalize_vendor_session(incident.incident_id, vendor_id, "DECLINED")
                self._timeline(
                    incident,
                    "vendor_dispatch_outcome",
                    "VENDOR_FALLBACK_ON_FAILURE",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id, "outcome": outcome},
                )
                self._notify(
                    incident,
                    f"{vendor.name} could not accept this repair; contacting the next eligible vendor.",
                    f"vendor-failed:{vendor.vendor_id}:decline",
                )
                self._dispatch_next(incident, request.event_id)
            else:
                self._timeline(
                    incident,
                    "vendor_response_ignored",
                    "VENDOR_RESPONSE_INVALID",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id, "outcome": outcome},
                )
        elif action == "vendor_quote":
            self._handle_vendor_quote(incident, request)
        elif action == "eta":
            if (
                incident.status not in {IncidentStatus.SCHEDULED, IncidentStatus.IN_PROGRESS}
                or not incident.assigned_vendor_id
            ):
                self._timeline(
                    incident,
                    "eta_ignored",
                    "ETA_REQUIRES_ASSIGNED_ACCEPTED_VENDOR",
                    event_id=request.event_id,
                )
                self._save(incident)
                return incident
            eta_minutes = request.payload.get("eta_minutes")
            if eta_minutes is not None:
                minutes = int(eta_minutes)
                if not 1 <= minutes <= 1440:
                    raise ValueError("ETA must be between 1 and 1440 minutes")
                incident.eta = self.clock.now() + timedelta(minutes=minutes)
                incident.eta_minutes = minutes
            vendor = next(
                (candidate for candidate in self.vendors if candidate.vendor_id == incident.assigned_vendor_id),
                None,
            )
            eta_text = (
                format_tenant_eta(
                    incident.eta,
                    incident.eta_minutes,
                    self.settings.display_timezone,
                    self.clock.now(),
                )
                if incident.eta
                else "being confirmed"
            )
            self._notify(
                incident,
                "🕒 Plumber ETA\n\n"
                f"ETA: {eta_text}\n"
                f"Vendor: {vendor.name if vendor else 'Assigned vendor'}",
                "eta-update",
            )
        elif action == "work_started":
            if incident.status == IncidentStatus.SCHEDULED and incident.assigned_vendor_id:
                vendor = next(
                    (
                        candidate
                        for candidate in self.vendors
                        if candidate.vendor_id == incident.assigned_vendor_id
                    ),
                    None,
                )
                if incident.approval and incident.approval.status == "pending":
                    self._timeline(
                        incident,
                        "work_started_blocked",
                        "SPENDING_APPROVAL_REQUIRED",
                        event_id=request.event_id,
                    )
                    if vendor:
                        self._notify_vendor(
                            incident,
                            vendor,
                            "Work cannot start until the manager approves the over-limit quote.",
                            "work-start-blocked-approval",
                        )
                else:
                    self._transition(
                        incident,
                        IncidentStatus.IN_PROGRESS,
                        "VENDOR_CHECK_IN_RECEIVED",
                        request.event_id,
                    )
            else:
                self._timeline(
                    incident,
                    "work_started_ignored",
                    "WORK_START_REQUIRES_SCHEDULED",
                    event_id=request.event_id,
                )
        elif action == "completion":
            self._handle_completion(incident, request)
        elif action == "tenant_confirm":
            if incident.status == IncidentStatus.PROVISIONALLY_RESOLVED:
                self._transition(
                    incident,
                    IncidentStatus.CLOSED,
                    "TENANT_CONFIRMATION_RECEIVED",
                    request.event_id,
                )
                incident.closure_reason = "tenant confirmed repair"
                self._timeline(
                    incident, "incident_closed", "CLOSURE_GATE_PASSED", event_id=request.event_id
                )
                self._notify(
                    incident,
                    "✅ Repair confirmed\n\n"
                    f"Thanks — the repair has been marked dry and incident {incident.incident_id} is now closed.",
                    "tenant-closure-confirmed",
                )
                vendor = next(
                    (candidate for candidate in self.vendors if candidate.vendor_id == incident.assigned_vendor_id),
                    None,
                )
                if vendor:
                    self._notify_vendor(
                        incident,
                        vendor,
                        "✅ Job completed\n\n"
                        f"The tenant confirmed the repair is dry. Incident {incident.incident_id} is now closed.",
                        "vendor-closure-confirmed",
                    )
            else:
                self._timeline(
                    incident,
                    "tenant_confirmation_deferred",
                    "CLOSURE_REQUIRES_VERIFICATION",
                    event_id=request.event_id,
                )
        elif action == "recurrence":
            self._handle_recurrence(incident, request.event_id)
        elif action == "approve":
            self._handle_approval(incident, request)
        elif action == "cancel":
            if incident.status not in {IncidentStatus.CLOSED, IncidentStatus.CANCELLED}:
                self._transition(
                    incident, IncidentStatus.CANCELLED, "MANAGER_CANCELLED", request.event_id
                )
            else:
                self._timeline(
                    incident, "cancel_ignored", "CANCEL_NOT_ALLOWED_IN_STATE", request.event_id
                )
        self._save(incident)
        return incident

    def request_tenant_confirmation(self, incident_id: str, event_id: str) -> Incident:
        """Send the delayed prompt; only a tenant response may close the incident."""

        incident = self._get(incident_id)
        if not self.repository.claim_event(event_id):
            return incident
        if incident.status == IncidentStatus.PROVISIONALLY_RESOLVED:
            self._timeline(
                incident,
                "tenant_confirmation_reminder_sent",
                "DELAYED_CONFIRMATION_REQUIRED",
                event_id=event_id,
            )
            self._notify(
                incident,
                "Please tap Dry now if the repair is holding, or Still leaking if it recurred.",
                "tenant-confirmation-reminder",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Dry now",
                                "callback_data": f"tenant:{incident.incident_id}:dry",
                            },
                            {
                                "text": "Still leaking",
                                "callback_data": f"tenant:{incident.incident_id}:leaking",
                            },
                        ]
                    ]
                },
            )
            self._save(incident)
        return incident

    def _handle_vendor_quote(self, incident: Incident, request: ActionRequest) -> None:
        if (
            not incident.work_order
            or incident.assigned_vendor_id != request.payload.get("vendor_id")
            or incident.status not in {IncidentStatus.SCHEDULED, IncidentStatus.IN_PROGRESS}
        ):
            self._timeline(
                incident,
                "vendor_quote_ignored",
                "VENDOR_QUOTE_NOT_ASSIGNED",
                event_id=request.event_id,
            )
            return
        amount = round(float(request.payload.get("amount", -1)), 2)
        if amount < 0:
            raise ValueError("vendor quote must be non-negative")
        incident.work_order.estimated_cost = amount
        if amount <= incident.work_order.authorized_amount:
            incident.work_order.status = "dispatched"
            self._timeline(
                incident,
                "vendor_quote_recorded",
                "QUOTE_WITHIN_AUTHORITY",
                event_id=request.event_id,
                metadata={"amount": amount, "currency": incident.work_order.currency},
            )
            return
        incident.approval = ApprovalRequest(
            approval_id=f"apr_{uuid4().hex[:12]}",
            incident_id=incident.incident_id,
            reason="vendor quote exceeds currently authorized amount",
            requested_amount=amount,
            limit=incident.work_order.authorized_amount,
            created_at=self.clock.now(),
        )
        incident.work_order.status = "approval_required"
        self._transition(
            incident, IncidentStatus.ESCALATED, "VENDOR_QUOTE_APPROVAL_REQUIRED", request.event_id
        )
        self._notify(
            incident,
            "The vendor quote exceeds the approved amount and needs manager approval.",
            "vendor-quote-approval",
        )
        vendor = next(
            (
                candidate
                for candidate in self.vendors
                if candidate.vendor_id == incident.assigned_vendor_id
            ),
            None,
        )
        if vendor:
            self._notify_vendor(
                incident,
                vendor,
                "This quote is above the S$250 autonomous limit. Do not start or continue work until manager approval.",
                "vendor-quote-over-limit",
            )

    def _handle_completion(self, incident: Incident, request: ActionRequest) -> None:
        if (
            incident.status
            not in {
                IncidentStatus.SCHEDULED,
                IncidentStatus.IN_PROGRESS,
            }
            or not incident.assigned_vendor_id
            or (incident.approval and incident.approval.status == "pending")
        ):
            self._timeline(
                incident,
                "completion_ignored",
                "COMPLETION_REQUIRES_ASSIGNED_ACTIVE_VENDOR",
                event_id=request.event_id,
            )
            return
        if incident.status == IncidentStatus.SCHEDULED:
            self._transition(
                incident,
                IncidentStatus.IN_PROGRESS,
                "VENDOR_CHECK_IN_IMPLIED_BY_COMPLETION",
                request.event_id,
            )
        evidence = CompletionEvidence.model_validate(request.payload)
        if evidence.photo:
            validate_media_asset(evidence.photo)
            evidence.photo.storage_uri = self.media_store.put(evidence.photo)
            if evidence.photo.asset_id not in incident.media_ids:
                incident.media_ids.append(evidence.photo.asset_id)
            vendor_id = incident.assigned_vendor_id or "vendor-unknown"
            completion_channel = (
                "telegram" if self.notifications.provider_name == "telegram" else "local_demo"
            )
            completion_status = "received" if completion_channel == "telegram" else "simulated"
            inbound_photo_exists = any(
                record.direction == "inbound"
                and record.sender_role == "vendor"
                and evidence.photo.asset_id in record.media_ids
                for record in self.repository.list_communications(incident.incident_id)
            )
            if not inbound_photo_exists:
                self.record_communication(
                    communication_id=f"comm:completion:{incident.incident_id}:{request.event_id}:image",
                    incident_id=incident.incident_id,
                    sender_role="vendor",
                    sender_id=vendor_id,
                    recipient_role="agent",
                    recipient_id="no-more-buckets",
                    channel=completion_channel,
                    direction="inbound",
                    message_type="image",
                    text="Completion evidence photo submitted.",
                    media_ids=[evidence.photo.asset_id],
                    provider_message_id=request.event_id,
                    delivery_status=completion_status,  # type: ignore[arg-type]
                )
        if evidence.invoice:
            vendor_id = incident.assigned_vendor_id or evidence.invoice.vendor_id
            completion_channel = (
                "telegram" if self.notifications.provider_name == "telegram" else "local_demo"
            )
            self.record_communication(
                communication_id=f"comm:completion:{incident.incident_id}:{request.event_id}:invoice",
                incident_id=incident.incident_id,
                sender_role="vendor",
                sender_id=vendor_id,
                recipient_role="agent",
                recipient_id="no-more-buckets",
                channel=completion_channel,
                direction="inbound",
                message_type="invoice",
                text=f"Invoice {evidence.invoice.invoice_id} submitted: {evidence.invoice.currency} {evidence.invoice.total:.2f}.",
                provider_message_id=request.event_id,
                delivery_status="received" if completion_channel == "telegram" else "simulated",
            )
        photo_facts = self.evidence_verifier.verify(evidence.photo, incident.work_order)
        assessment = assess_completion(
            evidence, photo_facts, incident.work_order, incident.assigned_vendor_id
        )
        incident.last_evidence = assessment
        self._timeline(
            incident,
            "completion_evidence_assessed",
            "EVIDENCE_GATE_BEFORE_CLOSURE",
            event_id=request.event_id,
            metadata={"passed": assessment.passed, "blocking_reasons": assessment.blocking_reasons},
        )
        if not assessment.passed:
            self._timeline(
                incident,
                "completion_retry_available",
                "EVIDENCE_GATE_BLOCKED_CLOSURE",
                event_id=request.event_id,
            )
            return
        self._transition(
            incident, IncidentStatus.VERIFYING, "COMPLETION_EVIDENCE_RECEIVED", request.event_id
        )
        if incident.work_order is not None:
            incident.work_order.status = "completed"
        incident.warranty_expires_at = self.clock.now() + (
            timedelta(seconds=self.settings.demo_warranty_period_seconds)
            if self.settings.demo_mode
            else timedelta(days=self.properties[incident.property_id].warranty_days)
        )
        self._transition(incident, IncidentStatus.PROVISIONALLY_RESOLVED, "EVIDENCE_GATE_PASSED")
        task_id = self._task_id("tenant-confirm", incident.incident_id)
        self._enqueue_task(
            task_id,
            incident.incident_id,
            "tenant_confirmation",
            {"incident_id": incident.incident_id},
            self.settings.tenant_confirmation_delay_seconds,
        )
        self._timeline(
            incident,
            "tenant_confirmation_scheduled",
            "DELAYED_CONFIRMATION_REQUIRED",
            metadata={"task_id": task_id},
        )
        self._notify(
            incident,
            "The repair evidence passed. Please confirm after you observe the fixture.",
            "tenant-confirmation-request",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Dry now",
                            "callback_data": f"tenant:{incident.incident_id}:dry",
                        },
                        {
                            "text": "Still leaking",
                            "callback_data": f"tenant:{incident.incident_id}:leaking",
                        },
                    ]
                ]
            },
        )

    def _handle_recurrence(self, incident: Incident, event_id: str) -> None:
        if incident.status not in {
            IncidentStatus.CLOSED,
            IncidentStatus.PROVISIONALLY_RESOLVED,
        }:
            self._timeline(
                incident, "recurrence_ignored", "WARRANTY_ONLY_REOPEN", event_id=event_id
            )
            return
        if incident.warranty_expires_at and self.clock.now() <= incident.warranty_expires_at:
            self._transition(incident, IncidentStatus.REOPENED, "WARRANTY_REOPEN", event_id)
            self._timeline(incident, "warranty_reopened", "WARRANTY_REOPEN", event_id=event_id)
            self._notify(
                incident,
                "The same issue recurred within the warranty window; reopening the original incident.",
                "warranty-reopen",
            )
            facts = incident.facts
            config = self.properties[incident.property_id]
            if not facts or evaluate_safety(facts).escalate:
                self._transition(
                    incident, IncidentStatus.ESCALATED, "WARRANTY_REOPEN_REQUIRES_REVIEW"
                )
                return
            incident.assigned_vendor_id = None
            incident.vendor_attempts = []
            incident.containment_instructions = property_specific_containment(config, facts)
            incident.work_order = build_bounded_work_order(incident, config, facts)
            incident.work_order_id = incident.work_order.work_order_id
            self._timeline(
                incident,
                "warranty_reopen_ready",
                "WARRANTY_REOPEN_REQUIRES_RESUMABLE_WORKFLOW",
                event_id=event_id,
            )
            if requires_spending_approval(incident.work_order):
                assert incident.work_order.estimated_cost is not None
                incident.approval = ApprovalRequest(
                    approval_id=f"apr_{uuid4().hex[:12]}",
                    incident_id=incident.incident_id,
                    reason="warranty recurrence estimate exceeds property spending limit",
                    requested_amount=incident.work_order.estimated_cost,
                    limit=incident.work_order.spending_limit,
                    created_at=self.clock.now(),
                )
                self._transition(
                    incident, IncidentStatus.ESCALATED, "SPENDING_LIMIT_APPROVAL_REQUIRED"
                )
                return
        else:
            self._timeline(
                incident, "recurrence_outside_warranty", "NEW_INCIDENT_REQUIRED", event_id=event_id
            )

    def _handle_approval(self, incident: Incident, request: ActionRequest) -> None:
        event_id = request.event_id
        if not incident.approval or incident.approval.status != "pending":
            self._timeline(incident, "approval_ignored", "NO_PENDING_APPROVAL", event_id=event_id)
            return
        requested_amount = incident.approval.requested_amount
        approved_amount = request.payload.get("approved_amount", requested_amount)
        try:
            approved_amount = round(float(approved_amount), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError("approved_amount must be numeric") from exc
        if approved_amount < requested_amount:
            raise ValueError("approved_amount cannot be below the requested amount")
        incident.approval.status = "approved"
        assert incident.work_order is not None
        incident.work_order.approved = True
        # Only this explicit manager action can expand authority above S$250;
        # safety escalation never routes through this path.
        incident.work_order.authorized_amount = approved_amount
        incident.work_order.status = "bounded"
        self._timeline(
            incident,
            "spending_approval_recorded",
            "MANAGER_EXPLICIT_AMOUNT_AUTHORITY",
            event_id=event_id,
            metadata={"approved_amount": approved_amount, "currency": incident.work_order.currency},
        )
        if incident.assigned_vendor_id:
            self._transition(
                incident, IncidentStatus.SCHEDULED, "MANAGER_APPROVED_OVER_LIMIT", event_id
            )
            vendor = next((candidate for candidate in self.vendors if candidate.vendor_id == incident.assigned_vendor_id), None)
            if vendor:
                session = self.repository.find_vendor_session(vendor.telegram_chat_id or "", vendor.vendor_id, incident.incident_id)
                if session and session.stage == "AWAITING_FINAL_APPROVAL":
                    session.final_price_confirmed = True
                    session.stage = "COMPLETION_REVIEW"
                    self._save_vendor_session(session)
                    self._notify_vendor(incident, vendor, "✅ Final price approved. Review the saved completion and submit when ready.", f"completion-approval-approved:{session.session_id}", {"inline_keyboard": [[{"text": "Submit completion", "callback_data": f"vs:{session.session_id}:cs"}]]})
            return
        self._transition(
            incident, IncidentStatus.DISPATCHING, "MANAGER_APPROVED_OVER_LIMIT", event_id
        )
        self._save(incident)
        self._dispatch_next(incident, f"approval-dispatch:{incident.incident_id}")

    def resume_pending_workflows(self) -> int:
        resumed = 0
        for incident in self.repository.list():
            if incident.status == IncidentStatus.REOPENED and incident.work_order:
                self._transition(incident, IncidentStatus.TRIAGED, "WARRANTY_REOPEN_RETRIAGED")
                self._transition(incident, IncidentStatus.CONTAINED, "WARRANTY_REOPEN_CONTAINMENT")
                if requires_spending_approval(incident.work_order):
                    resumed += 1
                    self._save(incident)
                    continue
                self._transition(
                    incident, IncidentStatus.DISPATCHING, "WARRANTY_REOPEN_DISPATCH_ALLOWED"
                )
                self._save(incident)
                self._dispatch_next(incident, f"warranty-dispatch:{incident.incident_id}")
                resumed += 1
            elif incident.status == IncidentStatus.DISPATCHING and incident.work_order:
                pending = next(
                    (
                        attempt
                        for attempt in incident.vendor_attempts
                        if attempt.outcome == "pending"
                    ),
                    None,
                )
                if pending:
                    task_id = self._task_id(
                        "vendor-timeout", incident.incident_id, pending.vendor_id
                    )
                    vendor = next(
                        (candidate for candidate in self.vendors if candidate.vendor_id == pending.vendor_id),
                        None,
                    )
                    remaining_seconds = (
                        self._vendor_timeout_seconds(incident, vendor)
                        if vendor is not None
                        else self.settings.routine_vendor_timeout_seconds
                    )
                    if pending.deadline_at:
                        remaining_seconds = max(
                            0, int((pending.deadline_at - self.clock.now()).total_seconds())
                        )
                    self._enqueue_task(
                        task_id,
                        incident.incident_id,
                        "vendor_timeout",
                        {"vendor_id": pending.vendor_id},
                        remaining_seconds,
                    )
                else:
                    self._dispatch_next(incident, f"restart-dispatch:{incident.incident_id}")
                resumed += 1
            elif incident.status == IncidentStatus.PROVISIONALLY_RESOLVED:
                task_id = self._task_id("tenant-confirm", incident.incident_id)
                self._enqueue_task(
                    task_id,
                    incident.incident_id,
                    "tenant_confirmation",
                    {"incident_id": incident.incident_id},
                    self.settings.tenant_confirmation_delay_seconds,
                )
                resumed += 1
        return resumed

    def list_incidents(self) -> list[Incident]:
        return self.repository.list()

    def get_incident(self, incident_id: str) -> Incident:
        return self._get(incident_id)

    def list_communications(self, incident_id: str) -> list[CommunicationRecord]:
        self._get(incident_id)
        return self.repository.list_communications(incident_id)
