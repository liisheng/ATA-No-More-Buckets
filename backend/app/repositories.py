from __future__ import annotations

import builtins
from copy import deepcopy
from datetime import datetime
from typing import Protocol

from .models import Incident, PairingCodeRecord, PropertyConfig, TenantContact, Vendor


class IncidentRepository(Protocol):
    provider_name: str

    def save(self, incident: Incident) -> None: ...

    def get(self, incident_id: str) -> Incident | None: ...

    def list(self) -> builtins.list[Incident]: ...

    def claim_idempotency(self, key: str, incident_id: str) -> str | None: ...

    def claim_event(self, event_id: str) -> bool: ...

    def seed_reference_data(
        self,
        properties: dict[str, PropertyConfig],
        vendors: builtins.list[Vendor],
        tenants: dict[str, TenantContact],
    ) -> None: ...

    def load_reference_data(
        self,
    ) -> tuple[dict[str, PropertyConfig], builtins.list[Vendor], dict[str, TenantContact]] | None: ...

    def create_pairing_code(self, record: PairingCodeRecord) -> None: ...

    def consume_pairing_code(
        self, code: str, telegram_chat_id: str, now: datetime
    ) -> PairingCodeRecord | None: ...

    def bind_telegram_chat(self, target_type: str, target_id: str, telegram_chat_id: str) -> None: ...


class InMemoryIncidentRepository:
    provider_name = "memory"

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._idempotency: dict[str, str] = {}
        self._events: set[str] = set()
        self._pairing_codes: dict[str, PairingCodeRecord] = {}

    def save(self, incident: Incident) -> None:
        self._incidents[incident.incident_id] = deepcopy(incident)

    def get(self, incident_id: str) -> Incident | None:
        incident = self._incidents.get(incident_id)
        return deepcopy(incident) if incident else None

    def list(self) -> builtins.list[Incident]:
        return [deepcopy(item) for item in self._incidents.values()]

    def claim_idempotency(self, key: str, incident_id: str) -> str | None:
        existing = self._idempotency.get(key)
        if existing:
            return existing
        self._idempotency[key] = incident_id
        return None

    def claim_event(self, event_id: str) -> bool:
        if event_id in self._events:
            return False
        self._events.add(event_id)
        return True

    def seed_reference_data(
        self,
        properties: dict[str, PropertyConfig],
        vendors: builtins.list[Vendor],
        tenants: dict[str, TenantContact],
    ) -> None:
        return None

    def load_reference_data(
        self,
    ) -> tuple[dict[str, PropertyConfig], builtins.list[Vendor], dict[str, TenantContact]] | None:
        return None

    def create_pairing_code(self, record: PairingCodeRecord) -> None:
        if record.code in self._pairing_codes:
            raise ValueError("pairing code already exists")
        self._pairing_codes[record.code] = deepcopy(record)

    def consume_pairing_code(
        self, code: str, telegram_chat_id: str, now: datetime
    ) -> PairingCodeRecord | None:
        record = self._pairing_codes.get(code)
        if not record or record.consumed_at or now > record.expires_at:
            return None
        record.consumed_at = now
        record.telegram_chat_id = telegram_chat_id
        return deepcopy(record)

    def bind_telegram_chat(self, target_type: str, target_id: str, telegram_chat_id: str) -> None:
        return None


class FirestoreIncidentRepository:
    provider_name = "firestore"

    def __init__(self, project: str | None, database: str) -> None:
        from google.cloud import firestore  # type: ignore[attr-defined]

        self._firestore = firestore
        self.client = firestore.Client(project=project, database=database)
        self.incidents = self.client.collection("incidents")
        self.timeline = self.client.collection("incident_timeline")
        self.idempotency = self.client.collection("idempotency_keys")
        self.events = self.client.collection("processed_events")
        self.reference_data = self.client.collection("reference_data")

    def save(self, incident: Incident) -> None:
        self.incidents.document(incident.incident_id).set(incident.model_dump(mode="json"))
        for entry in incident.timeline:
            try:
                self.timeline.document(entry.event_id).create(
                    {"incident_id": incident.incident_id, **entry.model_dump(mode="json")}
                )
            except Exception as exc:
                if exc.__class__.__name__ not in {"AlreadyExists", "Conflict"}:
                    raise

    def get(self, incident_id: str) -> Incident | None:
        snapshot = self.incidents.document(incident_id).get()
        if not snapshot.exists:
            return None
        return Incident.model_validate(snapshot.to_dict())

    def list(self) -> builtins.list[Incident]:
        return [Incident.model_validate(snapshot.to_dict()) for snapshot in self.incidents.stream()]

    def claim_idempotency(self, key: str, incident_id: str) -> str | None:
        doc = self.idempotency.document(key)
        snapshot = doc.get()
        if snapshot.exists:
            value = snapshot.to_dict() or {}
            return str(value.get("incident_id"))
        try:
            doc.create({"incident_id": incident_id})
        except Exception as exc:
            if exc.__class__.__name__ in {"AlreadyExists", "Conflict"}:
                value = doc.get().to_dict() or {}
                return str(value.get("incident_id"))
            raise
        return None

    def claim_event(self, event_id: str) -> bool:
        doc = self.events.document(event_id)
        try:
            doc.create({"processed": True})
            return True
        except Exception as exc:
            if exc.__class__.__name__ in {"AlreadyExists", "Conflict"}:
                return False
            raise

    def seed_reference_data(
        self,
        properties: dict[str, PropertyConfig],
        vendors: builtins.list[Vendor],
        tenants: dict[str, TenantContact],
    ) -> None:
        """Seed only synthetic configuration and Telegram chat IDs, never credentials."""
        for property_config in properties.values():
            self._create_reference_if_missing(
                f"property-{property_config.property_id}",
                {"kind": "property", **property_config.model_dump(mode="json")},
            )
        for vendor in vendors:
            self._create_reference_if_missing(
                f"vendor-{vendor.vendor_id}",
                {"kind": "vendor", **vendor.model_dump(mode="json")},
            )
        for tenant in tenants.values():
            self._create_reference_if_missing(
                f"tenant-{tenant.tenant_id}",
                {"kind": "tenant", **tenant.model_dump(mode="json")},
            )

    def _create_reference_if_missing(self, document_id: str, value: dict) -> None:
        try:
            self.reference_data.document(document_id).create(value)
        except Exception as exc:
            if exc.__class__.__name__ not in {"AlreadyExists", "Conflict"}:
                raise

    def load_reference_data(
        self,
    ) -> tuple[dict[str, PropertyConfig], builtins.list[Vendor], dict[str, TenantContact]] | None:
        properties: dict[str, PropertyConfig] = {}
        vendors: builtins.list[Vendor] = []
        tenants: dict[str, TenantContact] = {}
        for snapshot in self.reference_data.stream():
            value = snapshot.to_dict() or {}
            kind = value.get("kind")
            payload = {key: item for key, item in value.items() if key != "kind"}
            if kind == "property":
                config = PropertyConfig.model_validate(payload)
                properties[config.property_id] = config
            elif kind == "vendor":
                vendors.append(Vendor.model_validate(payload))
            elif kind == "tenant":
                tenant = TenantContact.model_validate(payload)
                tenants[tenant.tenant_id] = tenant
        if not properties or not vendors or not tenants:
            return None
        return properties, vendors, tenants

    def create_pairing_code(self, record: PairingCodeRecord) -> None:
        self.client.collection("pairing_codes").document(record.code).create(
            record.model_dump(mode="json")
        )

    def consume_pairing_code(
        self, code: str, telegram_chat_id: str, now: datetime
    ) -> PairingCodeRecord | None:
        ref = self.client.collection("pairing_codes").document(code)
        transaction = self.client.transaction()

        @self._firestore.transactional
        def consume(transaction):
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            record = PairingCodeRecord.model_validate(snapshot.to_dict())
            if record.consumed_at or now > record.expires_at:
                return None
            transaction.update(
                ref,
                {"consumed_at": now, "telegram_chat_id": telegram_chat_id},
            )
            record.consumed_at = now
            record.telegram_chat_id = telegram_chat_id
            return record

        return consume(transaction)

    def bind_telegram_chat(self, target_type: str, target_id: str, telegram_chat_id: str) -> None:
        self.reference_data.document(f"{target_type}-{target_id}").set(
            {"telegram_chat_id": telegram_chat_id}, merge=True
        )
