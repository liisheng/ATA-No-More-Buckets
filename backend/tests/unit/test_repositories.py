from __future__ import annotations

from dataclasses import dataclass

from app.catalog import demo_properties, demo_tenants, demo_vendors
from app.repositories import FirestoreIncidentRepository


@dataclass
class _Snapshot:
    value: dict

    def to_dict(self) -> dict:
        return self.value


class _ReferenceCollection:
    def __init__(self, values: list[dict]) -> None:
        self.values = values

    def stream(self) -> list[_Snapshot]:
        return [_Snapshot(value) for value in self.values]


def test_firestore_loads_legacy_paired_vendor_reference() -> None:
    property_value = {"kind": "property", **demo_properties()["demo-tampines-101"].model_dump(mode="json")}
    tenant_value = {"kind": "tenant", **demo_tenants()["tenant-demo-001"].model_dump(mode="json")}
    vendor_value = {"kind": "vendor", **demo_vendors()[1].model_dump(mode="json")}
    vendor_value.update(
        {
            "telegram_chat_id": "legacy-vendor-group",
            "authorized_telegram_user_ids": ["retired-user-field"],
        }
    )
    repository = object.__new__(FirestoreIncidentRepository)
    repository.reference_data = _ReferenceCollection(
        [property_value, tenant_value, vendor_value]
    )

    loaded = repository.load_reference_data()

    assert loaded is not None
    _, vendors, tenants = loaded
    vendor = next(item for item in vendors if item.vendor_id == "vendor-b")
    assert vendor.telegram_chat_id == "legacy-vendor-group"
    assert vendor.delivery_ready is False
    assert tenants["tenant-demo-001"].telegram_chat_id == "100000000001"
