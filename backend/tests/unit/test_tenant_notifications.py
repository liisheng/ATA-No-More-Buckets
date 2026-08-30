from datetime import UTC, datetime, timedelta

import pytest

from app.config import Settings
from app.models import ActionRequest, IncidentStatus, ReportInput
from app.service import format_tenant_eta


def test_tenant_eta_formatter_uses_singapore_12_hour_clock_and_duration() -> None:
    now = datetime(2026, 8, 30, 13, 18, tzinfo=UTC)
    assert format_tenant_eta(
        datetime(2026, 8, 30, 13, 28, tzinfo=UTC), 10, "Asia/Singapore", now
    ) == "9:28 PM (10 minutes)"


def test_tenant_eta_formatter_handles_tomorrow_and_later_dates() -> None:
    now = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)
    assert format_tenant_eta(
        datetime(2026, 8, 30, 16, 10, tzinfo=UTC), 20, "Asia/Singapore", now
    ) == "Tomorrow, 12:10 AM (20 minutes)"
    assert format_tenant_eta(
        datetime(2026, 9, 2, 2, 10, tzinfo=UTC), 20, "Asia/Singapore", now
    ) == "Sep 2, 10:10 AM (20 minutes)"


def test_display_timezone_is_validated() -> None:
    assert Settings(_env_file=None).display_timezone == "Asia/Singapore"
    with pytest.raises(ValueError, match="DISPLAY_TIMEZONE must be a valid IANA timezone"):
        Settings(_env_file=None, display_timezone="Not/A_Timezone")


def test_acceptance_copy_and_eta_message_are_tenant_safe(service, report_media) -> None:
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="Water is dripping under the kitchen sink.",
            media=[report_media],
            idempotency_key="tenant-notification-copy",
        )
    )
    acceptance = service.notifications.messages[-1].text
    assert acceptance == (
        "Vendor B · Blue Pipe Co. accepted your repair request. "
        "They’re confirming the arrival time now."
    )
    assert "Reply ETA" not in acceptance

    service.process_action(
        incident.incident_id,
        ActionRequest(
            action="eta",
            event_id="tenant-safe-eta",
            payload={"vendor_id": "vendor-b", "eta_minutes": 10},
        ),
    )
    eta_message = service.notifications.messages[-1].text
    assert "🕒 Plumber ETA" in eta_message
    assert "(10 minutes)" in eta_message
    assert "2026-" not in eta_message
    assert "+00:00" not in eta_message
    assert "Vendor: Vendor B · Blue Pipe Co." in eta_message


def test_tenant_confirmation_notifies_once_and_persists_messages(service, report_media) -> None:
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="Water is dripping under the kitchen sink.",
            media=[report_media],
            idempotency_key="tenant-closure-notification",
        )
    )
    incident.status = IncidentStatus.PROVISIONALLY_RESOLVED
    incident.assigned_vendor_id = "vendor-b"
    incident.warranty_expires_at = service.clock.now() + timedelta(days=1)
    service.repository.save(incident)

    closed = service.process_action(
        incident.incident_id,
        ActionRequest(action="tenant_confirm", event_id="tenant-close-once"),
    )
    assert closed.status == IncidentStatus.CLOSED
    messages = service.list_communications(incident.incident_id)
    outbound = [record for record in messages if record.direction == "outbound"]
    assert [record.recipient_role for record in outbound if "confirmed" in record.text.lower()] == [
        "tenant",
        "vendor",
    ]
    assert sum("repair has been marked dry" in record.text.lower() for record in outbound) == 1
    assert sum("tenant confirmed the repair is dry" in record.text for record in outbound) == 1

    service.process_action(
        incident.incident_id,
        ActionRequest(action="tenant_confirm", event_id="tenant-close-twice"),
    )
    outbound_after = [
        record for record in service.list_communications(incident.incident_id)
        if record.direction == "outbound"
    ]
    assert len(outbound_after) == len(outbound)


def test_tenant_confirmation_without_vendor_closes_safely(service, report_media) -> None:
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="Water is dripping under the kitchen sink.",
            media=[report_media],
            idempotency_key="tenant-closure-no-vendor",
        )
    )
    incident.status = IncidentStatus.PROVISIONALLY_RESOLVED
    incident.assigned_vendor_id = None
    service.repository.save(incident)
    closed = service.process_action(
        incident.incident_id,
        ActionRequest(action="tenant_confirm", event_id="tenant-close-no-vendor"),
    )
    assert closed.status == IncidentStatus.CLOSED
    assert any(record.recipient_role == "tenant" for record in service.list_communications(incident.incident_id))
