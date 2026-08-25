from __future__ import annotations

import hashlib

import pytest

from app.adapters import (
    DemoClock,
    DeterministicCompletionEvidenceVerifier,
    DeterministicFactExtractor,
    LocalDemoNotificationAdapter,
    LocalDemoVendorAdapter,
    LocalEventBus,
    LocalMediaStore,
    LocalTaskQueue,
    build_demo_media,
)
from app.config import Settings
from app.main import _demo_properties, _demo_tenants, _demo_vendors
from app.models import MediaAsset
from app.repositories import InMemoryIncidentRepository
from app.service import IncidentService


@pytest.fixture
def clock() -> DemoClock:
    return DemoClock()


@pytest.fixture
def service(clock: DemoClock) -> IncidentService:
    settings = Settings(
        app_env="test",
        demo_mode=True,
        storage_backend="memory",
        facts_provider="deterministic",
        tenant_confirmation_delay_seconds=1,
        spending_limit_default=250,
        warranty_days=30,
    )
    return IncidentService(
        settings=settings,
        repository=InMemoryIncidentRepository(),
        extractor=DeterministicFactExtractor(),
        notifications=LocalDemoNotificationAdapter(),
        vendors_adapter=LocalDemoVendorAdapter("decline"),
        evidence_verifier=DeterministicCompletionEvidenceVerifier(),
        media_store=LocalMediaStore(),
        event_bus=LocalEventBus(),
        tasks=LocalTaskQueue(),
        properties=_demo_properties(),
        vendors=_demo_vendors(),
        tenants=_demo_tenants(),
        clock=clock,
    )


@pytest.fixture
def report_media() -> MediaAsset:
    return build_demo_media("report-photo")


@pytest.fixture
def completion_media() -> MediaAsset:
    content = b"synthetic-image"
    return MediaAsset(
        asset_id="completion-photo",
        filename="after.jpg",
        mime_type="image/jpeg",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content_base64="c3ludGhldGljLWltYWdl",
        source="vendor",
    )
