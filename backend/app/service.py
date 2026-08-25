from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import uuid4

import structlog

from .adapters import (
    Clock,
    CompletionEvidenceVerifier,
    EventBus,
    FactExtractor,
    MediaStore,
    NotificationAdapter,
    NotificationMessage,
    SystemClock,
    TaskQueue,
    VendorAdapter,
    validate_media_asset,
)
from .config import Settings
from .models import (
    ActionRequest,
    ApprovalRequest,
    CompletionEvidence,
    Incident,
    IncidentStatus,
    PairingCodeRecord,
    PairingCodeResponse,
    PropertyConfig,
    ReportInput,
    RuntimeMetadata,
    TenantContact,
    TimelineEntry,
    Vendor,
    VendorAttempt,
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
            demo_clock_enabled=self.settings.demo_mode,
            demo_timings_seconds={
                "urgent_vendor_timeout": self.settings.urgent_vendor_timeout_seconds,
                "routine_vendor_timeout": self.settings.routine_vendor_timeout_seconds,
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
        self.started_telegram_chats.add(telegram_chat_id)
        return record

    def _save(self, incident: Incident) -> None:
        incident.updated_at = self.clock.now()
        incident.version += 1
        self.repository.save(incident)

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

    def _notify(self, incident: Incident, text: str, action_key: str) -> None:
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
        recipient_id = tenant.telegram_chat_id if tenant and tenant.telegram_chat_id else incident.tenant_id
        scoped_action_key = f"{incident.incident_id}:{action_key}"
        if self.notifications.provider_name == "telegram" and recipient_id not in self.started_telegram_chats:
            self._timeline(
                incident,
                "notification_blocked_until_start",
                "TELEGRAM_START_REQUIRED",
                metadata={"recipient_type": "tenant_or_vendor"},
            )
            return
        outcome = self.notifications.send(
            NotificationMessage(incident.incident_id, recipient_id, text, scoped_action_key)
        )
        self._timeline(
            incident, "notification_sent", "NOTIFY_DEDUPED_SAFE", metadata={"outcome": outcome}
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
            self.media_store.put(asset)
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
        self._timeline(incident, "report_received", "REPORT_ACCEPTED")
        incident.facts = self.extractor.extract(
            report.report_text, report.voice_transcript, report.media
        )
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

    def _vendor_timeout_seconds(self, incident: Incident) -> int:
        if incident.facts and incident.facts.severity.value in {"high", "critical"}:
            return self.settings.urgent_vendor_timeout_seconds
        return self.settings.routine_vendor_timeout_seconds

    def _accept_vendor(self, incident: Incident, vendor: Vendor, event_id: str) -> None:
        assert incident.work_order is not None
        incident.assigned_vendor_id = vendor.vendor_id
        incident.work_order.vendor_id = vendor.vendor_id
        incident.work_order.status = "dispatched"
        incident.eta = self.clock.now() + timedelta(minutes=20 if self.settings.demo_mode else 60)
        if incident.status != IncidentStatus.SCHEDULED:
            self._transition(incident, IncidentStatus.SCHEDULED, "VENDOR_ACCEPTED_ELIGIBLE", event_id)
        self._notify(
            incident,
            f"{vendor.name} accepted the bounded repair. ETA: {incident.eta.isoformat()}.",
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
        assert incident.work_order is not None
        try:
            result = self.vendors_adapter.dispatch(
                incident.work_order, vendor, f"dispatch:{incident.incident_id}:{vendor.vendor_id}"
            )
        except Exception:
            retry_count = sum(
                1
                for entry in incident.timeline
                if entry.kind == "vendor_provider_retry_scheduled"
                and entry.metadata.get("vendor_id") == vendor.vendor_id
            )
            if retry_count >= 3:
                self._transition(incident, IncidentStatus.ESCALATED, "VENDOR_PROVIDER_RETRY_EXHAUSTED")
                self._timeline(
                    incident,
                    "approval_required",
                    "VENDOR_PROVIDER_RETRY_EXHAUSTED",
                    metadata={"vendor_id": vendor.vendor_id},
                )
                self._save(incident)
                return
            retry_task_id = self._task_id("vendor-retry", incident.incident_id, vendor.vendor_id)
            self.tasks.enqueue(
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
        outcome = result.outcome
        attempt_outcome = {"accept": "accepted", "decline": "declined", "timeout": "timed_out"}.get(
            outcome, outcome
        )
        deadline = (
            self.clock.now() + timedelta(seconds=self._vendor_timeout_seconds(incident))
            if outcome == "pending"
            else None
        )
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
            self.tasks.enqueue(
                task_id,
                incident.incident_id,
                "vendor_timeout",
                {"vendor_id": vendor.vendor_id},
                self._vendor_timeout_seconds(incident),
            )
            self._timeline(
                incident,
                "vendor_timeout_scheduled",
                "VENDOR_RESPONSE_SLA",
                metadata={
                    "vendor_id": vendor.vendor_id,
                    "task_id": task_id,
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

    def process_action(self, incident_id: str, request: ActionRequest) -> Incident:
        # Validate nested untrusted input before it consumes the idempotency key.
        if request.action == "completion":
            CompletionEvidence.model_validate(request.payload)
        incident = self._get(incident_id)
        if not self.repository.claim_event(request.event_id):
            return incident
        action = request.action
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
                    incident, "vendor_retry_ignored", "VENDOR_RETRY_NOT_DISPATCHING", event_id=request.event_id
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
            if incident.assigned_vendor_id and incident.assigned_vendor_id != vendor_id:
                self._timeline(
                    incident,
                    "late_vendor_timeout_ignored",
                    "ASSIGNED_VENDOR_WINS_RACE",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id, "assigned_vendor_id": incident.assigned_vendor_id},
                )
            elif pending_attempt and incident.status == IncidentStatus.DISPATCHING:
                pending_attempt.outcome = "timed_out"
                self._timeline(
                    incident,
                    "vendor_timeout_received",
                    "VENDOR_FALLBACK_ON_FAILURE",
                    event_id=request.event_id,
                    metadata={"vendor_id": vendor_id},
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
            vendor = next((candidate for candidate in self.vendors if candidate.vendor_id == vendor_id), None)
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
            eta_minutes = request.payload.get("eta_minutes")
            if eta_minutes is not None:
                minutes = int(eta_minutes)
                if not 1 <= minutes <= 180:
                    raise ValueError("ETA must be between 1 and 180 minutes")
                incident.eta = self.clock.now() + timedelta(minutes=minutes)
            self._notify(
                incident,
                f"Your plumber ETA is {incident.eta.isoformat() if incident.eta else 'being confirmed'}.",
                "eta-update",
            )
        elif action == "work_started":
            self._transition(
                incident, IncidentStatus.IN_PROGRESS, "VENDOR_CHECK_IN_RECEIVED", request.event_id
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
            self._transition(
                incident, IncidentStatus.CANCELLED, "MANAGER_CANCELLED", request.event_id
            )
        self._save(incident)
        return incident

    def _handle_vendor_quote(self, incident: Incident, request: ActionRequest) -> None:
        if not incident.work_order or incident.assigned_vendor_id != request.payload.get("vendor_id"):
            self._timeline(
                incident, "vendor_quote_ignored", "VENDOR_QUOTE_NOT_ASSIGNED", event_id=request.event_id
            )
            return
        amount = round(float(request.payload.get("amount", -1)), 2)
        if amount < 0:
            raise ValueError("vendor quote must be non-negative")
        incident.work_order.estimated_cost = amount
        if amount <= incident.work_order.authorized_amount:
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
        self._transition(incident, IncidentStatus.ESCALATED, "VENDOR_QUOTE_APPROVAL_REQUIRED", request.event_id)
        self._notify(
            incident,
            "The vendor quote exceeds the approved amount and needs manager approval.",
            "vendor-quote-approval",
        )

    def _handle_completion(self, incident: Incident, request: ActionRequest) -> None:
        if incident.status == IncidentStatus.SCHEDULED:
            self._transition(
                incident,
                IncidentStatus.IN_PROGRESS,
                "VENDOR_CHECK_IN_IMPLIED_BY_COMPLETION",
                request.event_id,
            )
        self._transition(
            incident, IncidentStatus.VERIFYING, "COMPLETION_EVIDENCE_RECEIVED", request.event_id
        )
        evidence = CompletionEvidence.model_validate(request.payload)
        if evidence.photo:
            validate_media_asset(evidence.photo)
            self.media_store.put(evidence.photo)
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
            self._transition(incident, IncidentStatus.ESCALATED, "EVIDENCE_GATE_BLOCKED_CLOSURE")
            self._notify(
                incident,
                "We need corrected completion evidence before this incident can close.",
                "evidence-blocked",
            )
            return
        if incident.work_order is not None:
            incident.work_order.status = "completed"
        incident.warranty_expires_at = self.clock.now() + (
            timedelta(seconds=self.settings.demo_warranty_period_seconds)
            if self.settings.demo_mode
            else timedelta(days=self.properties[incident.property_id].warranty_days)
        )
        self._transition(incident, IncidentStatus.PROVISIONALLY_RESOLVED, "EVIDENCE_GATE_PASSED")
        task_id = self._task_id("tenant-confirm", incident.incident_id)
        self.tasks.enqueue(
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
        )

    def _handle_recurrence(self, incident: Incident, event_id: str) -> None:
        if incident.status != IncidentStatus.CLOSED:
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
                self._transition(incident, IncidentStatus.ESCALATED, "WARRANTY_REOPEN_REQUIRES_REVIEW")
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
                incident.approval = ApprovalRequest(
                    approval_id=f"apr_{uuid4().hex[:12]}",
                    incident_id=incident.incident_id,
                    reason="warranty recurrence estimate exceeds property spending limit",
                    requested_amount=incident.work_order.estimated_cost,
                    limit=incident.work_order.spending_limit,
                    created_at=self.clock.now(),
                )
                self._transition(incident, IncidentStatus.ESCALATED, "SPENDING_LIMIT_APPROVAL_REQUIRED")
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
            self._transition(incident, IncidentStatus.SCHEDULED, "MANAGER_APPROVED_OVER_LIMIT", event_id)
            return
        self._transition(incident, IncidentStatus.DISPATCHING, "MANAGER_APPROVED_OVER_LIMIT", event_id)
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
                self._transition(incident, IncidentStatus.DISPATCHING, "WARRANTY_REOPEN_DISPATCH_ALLOWED")
                self._save(incident)
                self._dispatch_next(incident, f"warranty-dispatch:{incident.incident_id}")
                resumed += 1
            elif incident.status == IncidentStatus.DISPATCHING and incident.work_order:
                pending = next(
                    (attempt for attempt in incident.vendor_attempts if attempt.outcome == "pending"), None
                )
                if pending:
                    task_id = self._task_id("vendor-timeout", incident.incident_id, pending.vendor_id)
                    remaining_seconds = self._vendor_timeout_seconds(incident)
                    if pending.deadline_at:
                        remaining_seconds = max(
                            0, int((pending.deadline_at - self.clock.now()).total_seconds())
                        )
                    self.tasks.enqueue(
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
                self.tasks.enqueue(
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
