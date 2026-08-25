"""Seed the single synthetic property and Telegram contacts into Firestore.

Usage from the repository root:
    PYTHONPATH=backend python -m app.seed_data

The CLI writes only synthetic catalog data and chat IDs. Bot tokens, webhook
secrets, and Gemini keys are never part of the seed payload.
"""

from .catalog import demo_properties, demo_tenants, demo_vendors
from .config import get_settings
from .repositories import FirestoreIncidentRepository


def seed() -> None:
    settings = get_settings()
    if not settings.google_cloud_project:
        raise SystemExit("GOOGLE_CLOUD_PROJECT is required")
    repository = FirestoreIncidentRepository(
        settings.google_cloud_project, settings.firestore_database
    )
    repository.seed_reference_data(demo_properties(), demo_vendors(), demo_tenants())
    print("Seeded one synthetic property, tenant contact, and vendor Telegram chat IDs.")


if __name__ == "__main__":
    seed()
