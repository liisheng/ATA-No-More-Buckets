from __future__ import annotations

import builtins
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

from .models import (
    CommunicationRecord,
    Incident,
    PairingCodeRecord,
    PropertyConfig,
    TelegramDraft,
    TenantContact,
    Vendor,
    VendorSession,
)


class IncidentRepository(Protocol):
    provider_name: str

    def save(self, incident: Incident) -> None: ...

    def get(self, incident_id: str) -> Incident | None: ...

    def list(self) -> builtins.list[Incident]: ...

    def save_communication(self, record: CommunicationRecord) -> None: ...

    def list_communications(self, incident_id: str) -> builtins.list[CommunicationRecord]: ...

    def save_draft(self, draft: TelegramDraft) -> None: ...

    def get_draft(self, draft_id: str) -> TelegramDraft | None: ...

    def list_drafts(self, telegram_chat_id: str) -> builtins.list[TelegramDraft]: ...

    def list_all_drafts(self) -> builtins.list[TelegramDraft]: ...

    def delete_draft(self, draft_id: str) -> None: ...

    def delete_communication(self, communication_id: str) -> None: ...

    def save_vendor_session(self, session: VendorSession) -> None: ...

    def get_vendor_session(self, session_id: str) -> VendorSession | None: ...

    def list_vendor_sessions(self, telegram_chat_id: str) -> builtins.list[VendorSession]: ...

    def find_vendor_session(self, telegram_chat_id: str, vendor_id: str, incident_id: str) -> VendorSession | None: ...

    def move_communications(self, source_incident_id: str, target_incident_id: str) -> None: ...

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
    ) -> (
        tuple[dict[str, PropertyConfig], builtins.list[Vendor], dict[str, TenantContact]] | None
    ): ...

    def create_pairing_code(self, record: PairingCodeRecord) -> None: ...

    def consume_pairing_code(
        self, code: str, telegram_chat_id: str, now: datetime
    ) -> PairingCodeRecord | None: ...

    def bind_telegram_chat(
        self, target_type: str, target_id: str, telegram_chat_id: str
    ) -> None: ...

    def add_vendor_telegram_user(self, vendor_id: str, telegram_user_id: str) -> None: ...

    def mark_telegram_delivery_ready(
        self, target_type: str, target_id: str, telegram_chat_id: str, started_at: datetime
    ) -> None: ...


class InMemoryIncidentRepository:
    provider_name = "memory"

    def __init__(self) -> None:
        self._incidents: dict[str, Incident] = {}
        self._idempotency: dict[str, str] = {}
        self._events: set[str] = set()
        self._pairing_codes: dict[str, PairingCodeRecord] = {}
        self._communications: dict[str, CommunicationRecord] = {}
        self._drafts: dict[str, TelegramDraft] = {}
        self._vendor_sessions: dict[str, VendorSession] = {}
        self._properties: dict[str, PropertyConfig] = {}
        self._vendors: builtins.list[Vendor] = []
        self._tenants: dict[str, TenantContact] = {}

    def save(self, incident: Incident) -> None:
        self._incidents[incident.incident_id] = deepcopy(incident)

    def get(self, incident_id: str) -> Incident | None:
        incident = self._incidents.get(incident_id)
        return deepcopy(incident) if incident else None

    def list(self) -> builtins.list[Incident]:
        return [deepcopy(item) for item in self._incidents.values()]

    def save_communication(self, record: CommunicationRecord) -> None:
        # Upsert by the stable communication ID so duplicate webhook/task delivery
        # cannot create a second visible contact. A later provider result may update
        # the delivery state without changing the record's identity.
        self._communications[record.communication_id] = deepcopy(record)

    def list_communications(self, incident_id: str) -> builtins.list[CommunicationRecord]:
        records = [
            deepcopy(record)
            for record in self._communications.values()
            if record.incident_id == incident_id
        ]
        return sorted(records, key=lambda record: record.timestamp)

    def save_draft(self, draft: TelegramDraft) -> None:
        self._drafts[draft.draft_id] = deepcopy(draft)

    def get_draft(self, draft_id: str) -> TelegramDraft | None:
        draft = self._drafts.get(draft_id)
        return deepcopy(draft) if draft else None

    def list_drafts(self, telegram_chat_id: str) -> builtins.list[TelegramDraft]:
        return sorted(
            [
                deepcopy(draft)
                for draft in self._drafts.values()
                if draft.telegram_chat_id == telegram_chat_id
            ],
            key=lambda draft: draft.updated_at,
            reverse=True,
        )

    def list_all_drafts(self) -> builtins.list[TelegramDraft]:
        return sorted(
            [deepcopy(draft) for draft in self._drafts.values()],
            key=lambda draft: draft.updated_at,
            reverse=True,
        )

    def delete_draft(self, draft_id: str) -> None:
        self._drafts.pop(draft_id, None)

    def delete_communication(self, communication_id: str) -> None:
        self._communications.pop(communication_id, None)

    def save_vendor_session(self, session: VendorSession) -> None:
        self._vendor_sessions[session.session_id] = deepcopy(session)

    def get_vendor_session(self, session_id: str) -> VendorSession | None:
        session = self._vendor_sessions.get(session_id)
        return deepcopy(session) if session else None

    def list_vendor_sessions(self, telegram_chat_id: str) -> builtins.list[VendorSession]:
        return sorted(
            [deepcopy(s) for s in self._vendor_sessions.values() if s.telegram_chat_id == telegram_chat_id],
            key=lambda s: s.updated_at,
            reverse=True,
        )

    def find_vendor_session(self, telegram_chat_id: str, vendor_id: str, incident_id: str) -> VendorSession | None:
        return next((session for session in self.list_vendor_sessions(telegram_chat_id) if session.vendor_id == vendor_id and session.incident_id == incident_id), None)

    def move_communications(self, source_incident_id: str, target_incident_id: str) -> None:
        for record in self._communications.values():
            if record.incident_id == source_incident_id:
                record.incident_id = target_incident_id

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
        if not self._properties:
            self._properties = deepcopy(properties)
        if not self._vendors:
            self._vendors = deepcopy(vendors)
        if not self._tenants:
            self._tenants = deepcopy(tenants)

    def load_reference_data(
        self,
    ) -> tuple[dict[str, PropertyConfig], builtins.list[Vendor], dict[str, TenantContact]] | None:
        if not self._properties or not self._vendors or not self._tenants:
            return None
        return deepcopy(self._properties), deepcopy(self._vendors), deepcopy(self._tenants)

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
        self._bind_target(target_type, target_id, telegram_chat_id)

    def add_vendor_telegram_user(self, vendor_id: str, telegram_user_id: str) -> None:
        for vendor in self._vendors:
            if vendor.vendor_id == vendor_id:
                vendor.authorized_telegram_user_ids.add(telegram_user_id)
                return

    def mark_telegram_delivery_ready(
        self, target_type: str, target_id: str, telegram_chat_id: str, started_at: datetime
    ) -> None:
        self._bind_target(target_type, target_id, telegram_chat_id)
        if target_type == "tenant" and target_id in self._tenants:
            self._tenants[target_id].telegram_started_at = started_at
            self._tenants[target_id].delivery_ready = True
        if target_type == "vendor":
            for vendor in self._vendors:
                if vendor.vendor_id == target_id:
                    vendor.telegram_started_at = started_at
                    vendor.delivery_ready = True

    def _bind_target(self, target_type: str, target_id: str, telegram_chat_id: str) -> None:
        if target_type == "tenant" and target_id in self._tenants:
            self._tenants[target_id].telegram_chat_id = telegram_chat_id
        if target_type == "vendor":
            for vendor in self._vendors:
                if vendor.vendor_id == target_id:
                    vendor.telegram_chat_id = telegram_chat_id


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
        self.communications = self.client.collection("communications")
        self.drafts = self.client.collection("telegram_drafts")
        self.vendor_sessions = self.client.collection("vendor_sessions")
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

    def save_communication(self, record: CommunicationRecord) -> None:
        self.communications.document(record.communication_id).set(record.model_dump(mode="json"))

    def list_communications(self, incident_id: str) -> builtins.list[CommunicationRecord]:
        records = []
        for snapshot in self.communications.stream():
            value = snapshot.to_dict() or {}
            if value.get("incident_id") == incident_id:
                records.append(CommunicationRecord.model_validate(value))
        return sorted(records, key=lambda record: record.timestamp)

    def save_draft(self, draft: TelegramDraft) -> None:
        self.drafts.document(draft.draft_id).set(draft.model_dump(mode="json"))

    def get_draft(self, draft_id: str) -> TelegramDraft | None:
        snapshot = self.drafts.document(draft_id).get()
        if not snapshot.exists:
            return None
        return TelegramDraft.model_validate(snapshot.to_dict())

    def list_drafts(self, telegram_chat_id: str) -> builtins.list[TelegramDraft]:
        drafts = []
        for snapshot in self.drafts.stream():
            value = snapshot.to_dict() or {}
            if value.get("telegram_chat_id") == telegram_chat_id:
                drafts.append(TelegramDraft.model_validate(value))
        return sorted(drafts, key=lambda draft: draft.updated_at, reverse=True)

    def list_all_drafts(self) -> builtins.list[TelegramDraft]:
        drafts = [
            TelegramDraft.model_validate(snapshot.to_dict() or {})
            for snapshot in self.drafts.stream()
        ]
        return sorted(drafts, key=lambda draft: draft.updated_at, reverse=True)

    def delete_draft(self, draft_id: str) -> None:
        self.drafts.document(draft_id).delete()

    def delete_communication(self, communication_id: str) -> None:
        self.communications.document(communication_id).delete()

    def save_vendor_session(self, session: VendorSession) -> None:
        self.vendor_sessions.document(session.session_id).set(session.model_dump(mode="json"))

    def get_vendor_session(self, session_id: str) -> VendorSession | None:
        snapshot = self.vendor_sessions.document(session_id).get()
        if not snapshot.exists:
            return None
        return VendorSession.model_validate(snapshot.to_dict())

    def list_vendor_sessions(self, telegram_chat_id: str) -> builtins.list[VendorSession]:
        sessions = []
        for snapshot in self.vendor_sessions.stream():
            value = snapshot.to_dict() or {}
            if value.get("telegram_chat_id") == telegram_chat_id:
                sessions.append(VendorSession.model_validate(value))
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    def find_vendor_session(self, telegram_chat_id: str, vendor_id: str, incident_id: str) -> VendorSession | None:
        return next((session for session in self.list_vendor_sessions(telegram_chat_id) if session.vendor_id == vendor_id and session.incident_id == incident_id), None)

    def move_communications(self, source_incident_id: str, target_incident_id: str) -> None:
        for snapshot in self.communications.stream():
            value = snapshot.to_dict() or {}
            if value.get("incident_id") == source_incident_id:
                value["incident_id"] = target_incident_id
                snapshot.reference.set(value)

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

        @self._firestore.transactional  # type: ignore[misc]
        def consume(transaction: Any) -> PairingCodeRecord | None:
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

        result: PairingCodeRecord | None = consume(transaction)
        return result

    def bind_telegram_chat(self, target_type: str, target_id: str, telegram_chat_id: str) -> None:
        self.reference_data.document(f"{target_type}-{target_id}").set(
            {"telegram_chat_id": telegram_chat_id}, merge=True
        )

    def add_vendor_telegram_user(self, vendor_id: str, telegram_user_id: str) -> None:
        from google.cloud.firestore_v1 import ArrayUnion

        self.reference_data.document(f"vendor-{vendor_id}").set(
            {"authorized_telegram_user_ids": ArrayUnion([telegram_user_id])}, merge=True
        )

    def mark_telegram_delivery_ready(
        self, target_type: str, target_id: str, telegram_chat_id: str, started_at: datetime
    ) -> None:
        self.reference_data.document(f"{target_type}-{target_id}").set(
            {
                "telegram_chat_id": telegram_chat_id,
                "telegram_started_at": started_at,
                "delivery_ready": True,
            },
            merge=True,
        )
