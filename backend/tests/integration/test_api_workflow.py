import base64
import json

from fastapi.testclient import TestClient

from app import main


def test_api_demo_seed_exposes_timeline_and_runtime(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    response = client.post("/api/demo/seed")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SCHEDULED"
    assert any(entry["kind"] == "vendor_dispatch_outcome" for entry in body["timeline"])
    runtime = client.get("/api/runtime").json()
    assert runtime["facts_provider"] == "deterministic"


def test_api_duplicate_action_delivery_does_not_duplicate_notification(
    monkeypatch, service
) -> None:
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    incident = client.post("/api/demo/seed").json()
    payload = {"action": "eta", "event_id": "api-duplicate-1", "payload": {}}
    first = client.post(f"/api/incidents/{incident['incident_id']}/actions", json=payload).json()
    second = client.post(f"/api/incidents/{incident['incident_id']}/actions", json=payload).json()
    assert len(second["timeline"]) == len(first["timeline"])


def test_pubsub_push_duplicate_is_claimed_once(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    incident = client.post("/api/demo/seed").json()
    body = base64.b64encode(
        json.dumps(
            {"event_id": "pubsub-duplicate-1", "incident_id": incident["incident_id"]}
        ).encode()
    ).decode()
    payload = {"message": {"messageId": "message-1", "data": body}}
    before = len(client.get(f"/api/incidents/{incident['incident_id']}").json()["timeline"])
    first = client.post("/api/events/pubsub", json=payload)
    second = client.post("/api/events/pubsub", json=payload)
    assert first.status_code == second.status_code == 200
    current = client.get(f"/api/incidents/{incident['incident_id']}").json()
    assert len(current["timeline"]) == before


def test_demo_replay_endpoints_are_disabled_for_live_service(monkeypatch, service) -> None:
    service.live_cloud = True
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    assert client.post("/api/demo/seed").status_code == 404
    assert client.post("/api/demo/reset").status_code == 404
    assert client.get("/api/drafts").status_code == 404


def test_task_callback_rejects_malformed_envelopes_before_audit(monkeypatch, service) -> None:
    monkeypatch.setattr(main, "service", service)
    client = TestClient(main.app)
    response = client.post(
        "/api/events/tasks",
        json={"task_id": "task-1", "incident_id": "incident-1", "task_type": "vendor_timeout", "payload": []},
    )
    assert response.status_code == 422
    assert not service.repository.list_communications("incident-1")
