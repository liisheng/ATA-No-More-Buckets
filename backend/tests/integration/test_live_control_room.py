from __future__ import annotations

from fastapi.testclient import TestClient

from app import main
from app.adapters import build_demo_voice_media
from app.models import ReportInput
from app.repositories import InMemoryIncidentRepository


def test_communications_are_persisted_with_provider_ids_and_are_upserted(
    service, report_media
) -> None:
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="Water is dripping under the kitchen sink.",
            media=[report_media],
            idempotency_key="communications-persisted",
        )
    )
    records = service.list_communications(incident.incident_id)
    assert any(
        record.sender_role == "tenant" and record.recipient_role == "agent" for record in records
    )
    assert any(
        record.sender_role == "agent" and record.recipient_role == "tenant" for record in records
    )
    assert any(
        record.sender_role == "agent" and record.recipient_role == "vendor" for record in records
    )
    assert all(record.provider_message_id for record in records if record.direction == "outbound")
    assert all(
        record.delivery_status == "simulated"
        for record in records
        if record.channel != "telegram" and record.direction == "outbound"
    )

    repository = service.repository
    assert isinstance(repository, InMemoryIncidentRepository)
    duplicate = records[0].model_copy()
    repository.save_communication(duplicate)
    assert len(repository.list_communications(incident.incident_id)) == len(records)


def test_media_descriptors_and_authorized_reads_include_audio_and_image(
    monkeypatch, service, report_media
) -> None:
    monkeypatch.setattr(main, "service", service)
    voice = build_demo_voice_media()
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="The sink is leaking; attached are a photo and voice note.",
            media=[report_media, voice],
            idempotency_key="media-control-room",
        )
    )
    client = TestClient(main.app)
    descriptors = client.get(f"/api/incidents/{incident.incident_id}/media")
    assert descriptors.status_code == 200
    descriptor_body = descriptors.json()
    assert {item["mime_type"] for item in descriptor_body} == {"image/png", "audio/wav"}
    audio_descriptor = next(item for item in descriptor_body if item["mime_type"] == "audio/wav")
    audio = client.get(audio_descriptor["url"])
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.content[:4] == b"RIFF"
    assert (
        client.get(f"/api/incidents/{incident.incident_id}/media/not-authorized").status_code == 404
    )
    assert client.get("/api/incidents/other-incident/media/" + voice.asset_id).status_code == 404


def test_live_api_exposes_new_incident_and_communications_without_refresh(
    monkeypatch, service, report_media
) -> None:
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    assert client.get("/api/incidents").json() == []
    incident = service.submit_report(
        ReportInput(
            property_id="demo-tampines-101",
            tenant_id="tenant-demo-001",
            report_text="A small leak is visible below the sink.",
            media=[report_media],
            idempotency_key="live-feed",
        )
    )
    incidents = client.get("/api/incidents")
    assert incidents.status_code == 200
    assert incidents.json()[0]["incident_id"] == incident.incident_id
    communications = client.get(f"/api/incidents/{incident.incident_id}/communications")
    assert communications.status_code == 200
    assert communications.json()[0]["incident_id"] == incident.incident_id
