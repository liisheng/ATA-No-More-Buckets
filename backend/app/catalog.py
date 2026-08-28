"""Synthetic reference catalog; chat IDs are data, never application secrets."""

from .models import PropertyConfig, TenantContact, Vendor


def demo_properties() -> dict[str, PropertyConfig]:
    return {
        "demo-tampines-101": PropertyConfig(
            property_id="demo-tampines-101",
            display_name="Tampines Grove · Unit 101",
            region="demo",
            currency="SGD",
            spending_limit=250,
            main_shutoff_location=(
                "under the kitchen sink: turn the blue-handled cold-water isolation valve clockwise"
            ),
            under_sink_valve_instructions=(
                "Under the kitchen sink, turn the blue-handled cold-water isolation valve clockwise."
            ),
            emergency_contact="Tampines Grove property manager",
            warranty_days=30,
        )
    }


def demo_vendors() -> list[Vendor]:
    return [
        Vendor(
            vendor_id="vendor-a",
            name="Vendor A · Apex Plumbing",
            region="demo",
            response_minutes=30,
            distance_km=4,
            demo_behavior="timeout",
            telegram_chat_id="-100000000101",
        ),
        Vendor(
            vendor_id="vendor-b",
            name="Vendor B · Blue Pipe Co.",
            region="demo",
            response_minutes=45,
            distance_km=7,
            demo_behavior="accept",
            telegram_chat_id="-100000000102",
        ),
    ]


def demo_tenants() -> dict[str, TenantContact]:
    return {
        "tenant-demo-001": TenantContact(
            tenant_id="tenant-demo-001",
            property_id="demo-tampines-101",
            display_name="Synthetic Tenant",
            telegram_chat_id="100000000001",
        )
    }
