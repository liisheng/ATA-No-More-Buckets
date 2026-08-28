import pytest

from app.adapters import VendorDispatchResult
from app.models import ActionRequest, IncidentStatus, Invoice, InvoiceLineItem, ReportInput


def make_report(
    report_media,
    text="Water is dripping under the kitchen sink; the cabinet is wet.",
    key="report-1",
):
    return ReportInput(
        property_id="demo-tampines-101",
        tenant_id="tenant-demo-001",
        report_text=text,
        voice_transcript="I can reach the shutoff and the outlet is dry.",
        media=[report_media],
        idempotency_key=key,
    )


def complete(service, incident, completion_media, event_id="complete-1"):
    invoice = Invoice(
        invoice_id="invoice-1",
        vendor_id=incident.assigned_vendor_id or "vendor-b",
        total=220,
        line_items=[
            InvoiceLineItem(description="leak repair labor and parts", quantity=1, unit_price=220)
        ],
    )
    incident = service.process_action(
        incident.incident_id, ActionRequest(action="work_started", event_id="start-1")
    )
    return service.process_action(
        incident.incident_id,
        ActionRequest(
            action="completion",
            event_id=event_id,
            payload={
                "photo": completion_media.model_dump(),
                "invoice": invoice.model_dump(),
            },
        ),
    )


def test_report_to_dispatch_falls_back_from_vendor_a_to_b(service, report_media) -> None:
    incident = service.submit_report(make_report(report_media))
    assert incident.status == IncidentStatus.SCHEDULED
    outcomes = [
        entry.metadata.get("outcome")
        for entry in incident.timeline
        if entry.kind == "vendor_dispatch_outcome"
    ]
    assert outcomes == ["decline", "accept"]
    assert incident.assigned_vendor_id == "vendor-b"


def test_late_vendor_a_acceptance_cannot_replace_vendor_b(service, report_media) -> None:
    incident = service.submit_report(make_report(report_media))
    updated = service.process_action(
        incident.incident_id,
        ActionRequest(action="vendor_a_late_accept", event_id="late-a-1"),
    )
    assert updated.assigned_vendor_id == "vendor-b"
    assert any(entry.kind == "late_vendor_acceptance_ignored" for entry in updated.timeline)


def test_vendor_response_sla_recovers_from_pending_timeout(service, report_media) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(make_report(report_media, key="timeout"))
    assert incident.status == IncidentStatus.DISPATCHING
    assert any(task["type"] == "vendor_timeout" for task in service.tasks.tasks.values())
    assert incident.vendor_attempts[0].deadline_at is not None
    service.clock.current = incident.vendor_attempts[0].deadline_at
    recovered = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_timeout",
            event_id="vendor-timeout-event",
            payload={"vendor_id": "vendor-a"},
        ),
    )
    assert recovered.status == IncidentStatus.SCHEDULED
    assert recovered.assigned_vendor_id == "vendor-b"


def test_early_vendor_timeout_reschedules_with_fresh_identity_and_preserves_deadline(
    service, report_media
) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(make_report(report_media, key="early-timeout"))
    original_task = next(
        task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout"
    )
    original_deadline = incident.vendor_attempts[0].deadline_at
    assert original_deadline is not None
    service.clock.advance(1)

    early = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_timeout",
            event_id=f"task-{original_task['task_id']}",
            payload={"vendor_id": "vendor-a"},
        ),
    )
    replacement_tasks = [
        task
        for task in service.tasks.tasks.values()
        if task["type"] == "vendor_timeout" and task["task_id"] != original_task["task_id"]
    ]
    assert early.status == IncidentStatus.DISPATCHING
    assert early.vendor_attempts[0].outcome == "pending"
    assert len(replacement_tasks) == 1
    replacement = replacement_tasks[0]
    assert replacement["task_id"] != original_task["task_id"]
    ignored = next(entry for entry in early.timeline if entry.kind == "vendor_timeout_ignored")
    assert ignored.metadata["replacement_task_id"] == replacement["task_id"]
    assert ignored.metadata["vendor_id"] == "vendor-a"
    assert ignored.metadata["original_deadline_at"] == original_deadline.isoformat()

    service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_timeout",
            event_id=f"task-{original_task['task_id']}",
            payload={"vendor_id": "vendor-a"},
        ),
    )
    assert len([
        task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout"
    ]) == 2

    service.clock.current = original_deadline
    recovered = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_timeout",
            event_id=f"task-{replacement['task_id']}",
            payload=replacement["payload"],
        ),
    )
    assert recovered.status == IncidentStatus.SCHEDULED
    assert recovered.assigned_vendor_id == "vendor-b"
    assert [a.outcome for a in recovered.vendor_attempts] == ["timed_out", "accepted"]
    assert len([entry for entry in recovered.timeline if entry.kind == "vendor_timeout_received"]) == 1


def test_early_timeout_queue_failure_leaves_source_event_retryable(service, report_media) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(make_report(report_media, key="queue-retry"))
    original_task = next(task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout")

    class FailingOnceQueue:
        provider_name = "local_tasks"

        def __init__(self, delegate):
            self.delegate = delegate
            self.failed = True

        def enqueue(self, *args, **kwargs):
            if self.failed:
                self.failed = False
                raise RuntimeError("temporary queue failure")
            return self.delegate.enqueue(*args, **kwargs)

    service.tasks = FailingOnceQueue(service.tasks)
    service.clock.advance(1)
    request = ActionRequest(
        action="vendor_timeout",
        event_id=f"task-{original_task['task_id']}",
        payload={"vendor_id": "vendor-a"},
    )
    with pytest.raises(RuntimeError):
        service.process_action(incident.incident_id, request)
    retried = service.process_action(incident.incident_id, request)
    assert retried.status == IncidentStatus.DISPATCHING
    assert len([task for task in service.tasks.delegate.tasks.values() if task["type"] == "vendor_timeout"]) == 2


def test_local_vendor_timeout_advances_only_demo_clock_to_persisted_deadline(
    service, report_media
) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(make_report(report_media, key="local-timer"))
    task = next(task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout")
    deadline = incident.vendor_attempts[0].deadline_at
    assert deadline is not None
    assert service.clock.now() < deadline
    service._run_local_task(task["task_id"], task["incident_id"], task["type"], task["payload"])
    assert service.clock.now() == deadline
    assert service.get_incident(incident.incident_id).assigned_vendor_id == "vendor-b"


def test_vendor_timeout_uses_urgent_demo_clock(service, report_media) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(
        make_report(
            report_media, "Water is gushing rapidly under the kitchen sink.", "urgent-timeout"
        )
    )
    timeout = next(
        task for task in service.tasks.tasks.values() if task["type"] == "vendor_timeout"
    )
    assert incident.facts and incident.facts.severity.value == "high"
    assert timeout["delay_seconds"] == 8


def test_persisted_communication_preserves_line_breaks(service) -> None:
    record = service.record_communication(
        communication_id="comm:formatting",
        incident_id="inc-formatting",
        sender_role="tenant",
        sender_id="tenant-demo-001",
        recipient_role="agent",
        recipient_id="no-more-buckets",
        channel="telegram",
        direction="inbound",
        message_type="text",
        text="Line one\r\n\r\nLine   two\x00\n\n\nLine three",
    )
    assert record.text == "Line one\n\nLine two\n\nLine three"


def test_transient_vendor_provider_failure_is_retried(service, report_media) -> None:
    class FlakyVendorAdapter:
        calls = 0

        def dispatch(self, work_order, vendor, idempotency_key):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic transient provider failure")
            return VendorDispatchResult("accept", "provider-recovered")

    service.vendors_adapter = FlakyVendorAdapter()
    incident = service.submit_report(make_report(report_media, key="provider-retry"))
    assert incident.status == IncidentStatus.DISPATCHING
    retry = next(task for task in service.tasks.tasks.values() if task["type"] == "vendor_retry")
    recovered = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_retry",
            event_id=f"task-{retry['task_id']}",
            payload={"vendor_id": "vendor-a"},
        ),
    )
    assert recovered.status == IncidentStatus.SCHEDULED


def test_duplicate_report_and_event_delivery_are_idempotent(service, report_media) -> None:
    first = service.submit_report(make_report(report_media, key="same-key"))
    second = service.submit_report(make_report(report_media, key="same-key"))
    assert first.incident_id == second.incident_id
    before = len(first.timeline)
    action = ActionRequest(action="eta", event_id="duplicate-event")
    service.process_action(first.incident_id, action)
    after_first = service.get_incident(first.incident_id)
    service.process_action(first.incident_id, action)
    after_second = service.get_incident(first.incident_id)
    assert len(after_first.timeline) == before + 1
    assert len(after_second.timeline) == len(after_first.timeline)


def test_over_limit_work_requires_approval_before_dispatch(service, report_media) -> None:
    incident = service.submit_report(
        make_report(
            report_media, "Water is gushing under the ceiling; expected quote $1200.", "over-limit"
        )
    )
    assert incident.status == IncidentStatus.ESCALATED
    assert incident.approval and incident.approval.status == "pending"
    assert not incident.assigned_vendor_id
    approved = service.process_action(
        incident.incident_id, ActionRequest(action="approve", event_id="approve-1")
    )
    assert approved.status == IncidentStatus.SCHEDULED
    assert approved.assigned_vendor_id == "vendor-b"


def test_unknown_initial_cost_dispatches_and_quote_enforces_cap(service, report_media) -> None:
    incident = service.submit_report(make_report(report_media, "Water is dripping under the sink.", "unknown-cost"))
    assert incident.status == IncidentStatus.SCHEDULED
    assert incident.work_order and incident.work_order.estimated_cost is None
    assert incident.work_order.authorized_amount == 250
    within_limit = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_quote",
            event_id="quote-220",
            payload={"vendor_id": "vendor-b", "amount": 220},
        ),
    )
    assert within_limit.status == IncidentStatus.SCHEDULED
    assert within_limit.work_order and within_limit.work_order.estimated_cost == 220
    exact_limit = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_quote",
            event_id="quote-250",
            payload={"vendor_id": "vendor-b", "amount": 250},
        ),
    )
    assert exact_limit.status == IncidentStatus.SCHEDULED
    over_limit = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="vendor_quote",
            event_id="quote-300",
            payload={"vendor_id": "vendor-b", "amount": 300},
        ),
    )
    assert over_limit.status == IncidentStatus.ESCALATED
    assert over_limit.approval and over_limit.approval.status == "pending"
    blocked = service.process_action(
        incident.incident_id,
        ActionRequest(action="work_started", event_id="quote-start-blocked"),
    )
    assert blocked.status == IncidentStatus.ESCALATED
    assert any(entry.kind == "work_started_ignored" for entry in blocked.timeline)


def test_invalid_eta_completion_and_confirmation_do_not_change_state(service, report_media) -> None:
    service.vendors_adapter.vendor_a_behavior = "pending"
    incident = service.submit_report(make_report(report_media, key="preconditions"))
    assert incident.status == IncidentStatus.DISPATCHING
    before = incident.status
    eta = service.process_action(
        incident.incident_id,
        ActionRequest(action="eta", event_id="eta-before-vendor", payload={"eta_minutes": 20}),
    )
    assert eta.status == before
    completion = service.process_action(
        incident.incident_id,
        ActionRequest(action="completion", event_id="completion-before-vendor", payload={}),
    )
    assert completion.status == before
    confirmed = service.process_action(
        incident.incident_id,
        ActionRequest(action="tenant_confirm", event_id="confirm-before-close"),
    )
    assert confirmed.status == before
    assert any(entry.kind == "eta_ignored" for entry in confirmed.timeline)
    assert any(entry.kind == "completion_ignored" for entry in confirmed.timeline)


def test_escalated_incident_cannot_accept_completion_or_confirmation(service, report_media) -> None:
    incident = service.submit_report(
        make_report(report_media, "Water is beside a sparking outlet; danger.", "unsafe-close")
    )
    assert incident.status == IncidentStatus.ESCALATED
    blocked = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="completion",
            event_id="completion-escalated",
            payload={"photo": None, "invoice": None},
        ),
    )
    confirmed = service.process_action(
        incident.incident_id,
        ActionRequest(action="tenant_confirm", event_id="confirm-escalated"),
    )
    assert blocked.status == confirmed.status == IncidentStatus.ESCALATED
    assert any(entry.kind == "completion_ignored" for entry in blocked.timeline)
    assert any(entry.kind == "tenant_confirmation_deferred" for entry in confirmed.timeline)


def test_explicit_approval_sets_authority_and_never_happens_for_safety(
    service, report_media
) -> None:
    over_limit = service.submit_report(
        make_report(
            report_media,
            "Water is gushing under the sink; expected quote $1200.",
            "approval-amount",
        )
    )
    approved = service.process_action(
        over_limit.incident_id,
        ActionRequest(
            action="approve",
            event_id="approval-amount-event",
            payload={"approved_amount": 1250},
        ),
    )
    assert approved.work_order and approved.work_order.authorized_amount == 1250
    assert approved.work_order.spending_limit == 250

    unsafe = service.submit_report(
        make_report(report_media, "Water is beside a sparking outlet; danger.", "safety-no-expand")
    )
    ignored = service.process_action(
        unsafe.incident_id, ActionRequest(action="approve", event_id="unsafe-approval")
    )
    assert ignored.status == IncidentStatus.ESCALATED
    assert ignored.work_order is None


def test_missing_or_mismatched_evidence_cannot_close(
    service, report_media, completion_media
) -> None:
    incident = service.submit_report(make_report(report_media, key="evidence"))
    incident = service.process_action(
        incident.incident_id, ActionRequest(action="work_started", event_id="evidence-start")
    )
    incident = service.process_action(
        incident.incident_id,
        ActionRequest(
            action="completion",
            event_id="evidence-bad",
            payload={},
        ),
    )
    assert incident.status == IncidentStatus.ESCALATED
    assert incident.last_evidence and not incident.last_evidence.passed


def test_warranty_recurrence_reopens_original_incident(
    service, report_media, completion_media, clock
) -> None:
    incident = service.submit_report(make_report(report_media, key="warranty"))
    incident = complete(service, incident, completion_media)
    assert incident.status == IncidentStatus.PROVISIONALLY_RESOLVED
    reminder = service.request_tenant_confirmation(incident.incident_id, "tenant-reminder-1")
    assert reminder.status == IncidentStatus.PROVISIONALLY_RESOLVED
    incident = service.process_action(
        incident.incident_id, ActionRequest(action="tenant_confirm", event_id="confirm-1")
    )
    assert incident.status == IncidentStatus.CLOSED
    clock.advance(20)
    incident = service.process_action(
        incident.incident_id, ActionRequest(action="recurrence", event_id="recurrence-1")
    )
    assert incident.status == IncidentStatus.REOPENED


def test_restart_resumes_dispatch_and_delayed_confirmation(service, report_media) -> None:
    incident = service.submit_report(make_report(report_media, key="restart"))
    incident.status = IncidentStatus.DISPATCHING
    incident.assigned_vendor_id = None
    incident.vendor_attempts = []
    assert incident.work_order is not None
    incident.work_order.vendor_id = None
    service.repository.save(incident)
    resumed = service.resume_pending_workflows()
    recovered = service.get_incident(incident.incident_id)
    assert resumed == 1
    assert recovered.status == IncidentStatus.SCHEDULED
    assert recovered.assigned_vendor_id == "vendor-b"
