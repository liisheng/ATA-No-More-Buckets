from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IncidentStatus(StrEnum):
    REPORTED = "REPORTED"
    TRIAGED = "TRIAGED"
    CONTAINED = "CONTAINED"
    DISPATCHING = "DISPATCHING"
    SCHEDULED = "SCHEDULED"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    PROVISIONALLY_RESOLVED = "PROVISIONALLY_RESOLVED"
    CLOSED = "CLOSED"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"
    REOPENED = "REOPENED"


class IssueType(StrEnum):
    LEAK = "leak"
    FLOOD = "flood"
    DRAIN = "drain"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MediaAsset(StrictModel):
    asset_id: str
    filename: str = Field(min_length=1, max_length=180)
    mime_type: str
    size_bytes: int = Field(ge=0)
    sha256: str
    duration_seconds: int | None = Field(default=None, ge=0)
    content_base64: str | None = None
    storage_uri: str | None = None
    source: Literal["tenant", "vendor", "system"] = "tenant"


class InvoiceLineItem(StrictModel):
    description: str = Field(min_length=1, max_length=200)
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)

    @property
    def total(self) -> float:
        return round(self.quantity * self.unit_price, 2)


class Invoice(StrictModel):
    invoice_id: str
    vendor_id: str
    currency: str = "SGD"
    total: float = Field(ge=0)
    line_items: list[InvoiceLineItem] = Field(min_length=1)

    @model_validator(mode="after")
    def total_matches_line_items(self) -> Invoice:
        line_total = round(sum(item.total for item in self.line_items), 2)
        if round(self.total, 2) != line_total:
            raise ValueError("invoice total must equal the line-item total")
        return self


class ObservableFacts(StrictModel):
    """Schema returned by Gemini; only observable facts, never authorization decisions."""

    issue_type: IssueType = IssueType.UNKNOWN
    severity: Severity = Severity.MEDIUM
    water_visible: bool = False
    water_source: str | None = Field(default=None, max_length=200)
    electrical_hazard: bool = False
    structural_hazard: bool = False
    gas_hazard: bool = False
    occupant_danger: bool = False
    uncontrolled_flooding: bool = False
    access_available: bool = True
    estimated_cost: float | None = Field(default=None, ge=0)
    affected_rooms: list[str] = Field(default_factory=list, max_length=10)
    observed_text: str = Field(default="", max_length=4000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    source_confidence: float = Field(default=0.0, ge=0, le=1)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)


class ReportAssessment(StrictModel):
    """Schema-validated multimodal observations; policy remains deterministic code."""

    voice_transcript: str | None = Field(default=None, max_length=4000)
    facts: ObservableFacts
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    missing_information: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(default=0.0, ge=0, le=1)


class CommunicationRecord(StrictModel):
    """A persisted contact record, including simulated contacts in demo mode."""

    communication_id: str = Field(default_factory=lambda: f"comm_{uuid4().hex[:16]}")
    incident_id: str
    sender_role: Literal["tenant", "agent", "vendor", "scheduler", "system"]
    sender_id: str
    recipient_role: Literal["tenant", "agent", "vendor", "scheduler", "system"]
    recipient_id: str
    channel: str = Field(min_length=1, max_length=80)
    direction: Literal["inbound", "outbound"]
    message_type: Literal["text", "image", "video", "audio", "button", "invoice", "system"]
    text: str = Field(default="", max_length=4000)
    media_ids: list[str] = Field(default_factory=list, max_length=20)
    provider_message_id: str | None = Field(default=None, max_length=200)
    delivery_status: Literal["received", "sent", "delivered", "failed", "simulated", "deduplicated"]
    timestamp: datetime


class MediaDescriptor(StrictModel):
    """Safe client-facing media metadata; storage URIs never leave the backend."""

    media_id: str
    filename: str
    mime_type: str
    size_bytes: int = Field(ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    source: Literal["tenant", "vendor", "system"]
    url: str


class PropertyConfig(StrictModel):
    property_id: str
    display_name: str
    region: str = "demo"
    currency: str = "SGD"
    spending_limit: float = Field(default=250, ge=0)
    main_shutoff_location: str = "under the kitchen sink"
    under_sink_valve_instructions: str = (
        "Under the kitchen sink, turn the blue-handled cold-water isolation valve clockwise."
    )
    emergency_contact: str = "property manager"
    warranty_days: int = Field(default=30, ge=0)


class Vendor(StrictModel):
    vendor_id: str
    name: str
    region: str
    trades: set[str] = Field(default_factory=lambda: {"plumbing"})
    active: bool = True
    insured: bool = True
    response_minutes: int = Field(default=60, ge=0)
    distance_km: float = Field(default=5, ge=0)
    demo_behavior: Literal["accept", "decline", "timeout"] = "accept"
    telegram_chat_id: str | None = None
    telegram_started_at: datetime | None = None
    delivery_ready: bool = False


class TenantContact(StrictModel):
    tenant_id: str
    property_id: str
    display_name: str
    telegram_chat_id: str | None = None
    telegram_started_at: datetime | None = None
    delivery_ready: bool = False


class PairingCodeRecord(StrictModel):
    code: str = Field(min_length=16, max_length=120)
    target_type: Literal["tenant", "vendor"]
    target_id: str = Field(min_length=1, max_length=120)
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    telegram_chat_id: str | None = None


class PairingCodeRequest(StrictModel):
    target_type: Literal["tenant", "vendor"]
    target_id: str = Field(min_length=1, max_length=120)


class PairingCodeResponse(StrictModel):
    code: str
    deep_link: str
    target_type: Literal["tenant", "vendor"]
    target_id: str
    expires_at: datetime


class TelegramDraftItem(StrictModel):
    """One ordered, idempotent message contribution to a Telegram draft."""

    item_key: str = Field(min_length=1, max_length=240)
    kind: Literal["text", "image", "video", "audio"]
    text: str = Field(default="", max_length=4000)
    media_id: str | None = None
    communication_id: str


class TelegramDraft(StrictModel):
    """A persisted, expiring tenant report assembled from Telegram updates."""

    draft_id: str
    tenant_id: str
    property_id: str
    telegram_chat_id: str
    text_parts: list[str] = Field(default_factory=list, max_length=20)
    media: list[MediaAsset] = Field(default_factory=list, max_length=10)
    items: list[TelegramDraftItem] = Field(default_factory=list, max_length=20)
    item_keys: list[str] = Field(default_factory=list, max_length=20)
    communication_ids: list[str] = Field(default_factory=list, max_length=20)
    submitted_incident_id: str | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    revision: int = Field(default=0, ge=0)


class VendorSession(StrictModel):
    """Persisted Telegram wizard state for one vendor and incident."""

    session_id: str = Field(default_factory=lambda: f"vs_{uuid4().hex[:20]}")
    incident_id: str
    vendor_id: str
    telegram_chat_id: str
    stage: Literal[
        "OFFERED",
        "AWAITING_PRICE",
        "CONFIRMING_PRICE",
        "AWAITING_ETA",
        "CONFIRMING_ETA",
        "REVIEW",
        "SUBMITTED",
        "AWAITING_PHOTO",
        "AWAITING_SCOPE",
        "COMPLETION_REVIEW",
        "CONFIRMING_FINAL_PRICE",
        "CANCELLED",
    ] = "OFFERED"
    draft_price: float | None = Field(default=None, ge=0)
    price_confirmed: bool = False
    draft_eta: int | None = Field(default=None, ge=1, le=1440)
    eta_confirmed: bool = False
    completion_photo_ids: list[str] = Field(default_factory=list, max_length=5)
    completion_scope: str | None = Field(default=None, max_length=500)
    final_price: float | None = Field(default=None, ge=0)
    final_price_confirmed: bool = False
    revision: int = Field(default=0, ge=0)
    submitted: bool = False
    cancelled: bool = False
    created_at: datetime
    updated_at: datetime


class VendorAttempt(StrictModel):
    vendor_id: str
    outcome: Literal["pending", "accepted", "declined", "timed_out"]
    attempt_id: str
    event_id: str
    at: datetime
    deadline_at: datetime | None = None


class WorkOrder(StrictModel):
    work_order_id: str
    incident_id: str
    property_name: str | None = None
    scope: str
    currency: str
    spending_limit: float = Field(ge=0)
    estimated_cost: float | None = Field(default=None, ge=0)
    authorized_amount: float = Field(default=0, ge=0)
    status: Literal["bounded", "approval_required", "dispatched", "completed"] = "bounded"
    approved: bool = False
    vendor_id: str | None = None


class ApprovalRequest(StrictModel):
    approval_id: str
    incident_id: str
    reason: str
    requested_amount: float = Field(ge=0)
    limit: float = Field(ge=0)
    status: Literal["pending", "approved", "rejected"] = "pending"
    created_at: datetime


class TimelineEntry(StrictModel):
    event_id: str
    at: datetime
    kind: str
    rule_id: str | None = None
    state_from: IncidentStatus | None = None
    state_to: IncidentStatus | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompletionEvidence(StrictModel):
    photo: MediaAsset | None = None
    invoice: Invoice | None = None


class CompletionPhotoFacts(StrictModel):
    """Observable photo facts extracted by a verifier; never supplied by a webhook caller."""

    photo_matches: bool = False
    photo_match_confidence: float = Field(default=0.0, ge=0, le=1)


class EvidenceAssessment(StrictModel):
    photo_present: bool
    photo_matches: bool
    photo_confidence: float
    invoice_present: bool
    invoice_scope_match: bool
    invoice_total: float | None
    within_spending_limit: bool
    passed: bool
    blocking_reasons: list[str] = Field(default_factory=list)


class Incident(StrictModel):
    incident_id: str
    property_id: str
    tenant_id: str
    status: IncidentStatus = IncidentStatus.REPORTED
    report_text: str = ""
    voice_transcript: str | None = None
    media_ids: list[str] = Field(default_factory=list)
    report_assessment: ReportAssessment | None = None
    facts: ObservableFacts | None = None
    containment_instructions: str | None = None
    work_order_id: str | None = None
    work_order: WorkOrder | None = None
    assigned_vendor_id: str | None = None
    vendor_attempts: list[VendorAttempt] = Field(default_factory=list)
    approval: ApprovalRequest | None = None
    last_evidence: EvidenceAssessment | None = None
    eta: datetime | None = None
    warranty_expires_at: datetime | None = None
    closure_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    version: int = Field(default=0, ge=0)
    timeline: list[TimelineEntry] = Field(default_factory=list)


class ReportInput(StrictModel):
    property_id: str
    tenant_id: str
    report_text: str = Field(min_length=1, max_length=4000)
    voice_transcript: str | None = Field(default=None, max_length=4000)
    media: list[MediaAsset] = Field(default_factory=list, max_length=5)
    idempotency_key: str | None = Field(default=None, max_length=120)
    source_channel: str = Field(default="local_demo", max_length=80)
    provider_message_id: str | None = Field(default=None, max_length=200)


class ActionRequest(StrictModel):
    action: Literal[
        "vendor_a_late_accept",
        "vendor_timeout",
        "vendor_response",
        "vendor_quote",
        "vendor_retry",
        "eta",
        "work_started",
        "completion",
        "tenant_confirm",
        "recurrence",
        "approve",
        "cancel",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    event_id: str = Field(min_length=1, max_length=160)


class TaskEvent(StrictModel):
    task_id: str = Field(min_length=1, max_length=200)
    incident_id: str = Field(min_length=1, max_length=100)
    task_type: Literal["tenant_confirmation", "vendor_timeout", "vendor_retry"]
    payload: dict[str, Any] = Field(default_factory=dict)


class RuntimeMetadata(StrictModel):
    environment: str
    deployment: str
    facts_provider: str
    facts_model: str
    storage_backend: str
    eventing: str
    messaging_provider: str
    demo_clock_enabled: bool
    demo_timings_seconds: dict[str, int] = Field(default_factory=dict)
    synthetic_data_only: bool = True


def redact_for_log(value: str, max_length: int = 80) -> str:
    """Return a bounded, non-sensitive label for structured logs."""

    cleaned = " ".join(value.split())
    return cleaned[:max_length]
