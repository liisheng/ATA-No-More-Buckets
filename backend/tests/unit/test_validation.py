import hashlib

import pytest
from pydantic import ValidationError

from app.adapters import validate_media_asset
from app.models import MediaAsset, ObservableFacts


def test_observable_fact_schema_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ObservableFacts.model_validate({"issue_type": "leak", "not_authorization": True})


def test_media_validation_rejects_unsupported_type_and_bad_digest() -> None:
    with pytest.raises(ValueError, match="unsupported media"):
        validate_media_asset(
            MediaAsset(
                asset_id="x", filename="x.exe", mime_type="application/exe", size_bytes=0, sha256=""
            )
        )
    with pytest.raises(ValueError, match="digest"):
        validate_media_asset(
            MediaAsset(
                asset_id="x",
                filename="x.jpg",
                mime_type="image/jpeg",
                size_bytes=3,
                sha256="bad",
                content_base64="YWJj",
            )
        )


def test_media_validation_accepts_matching_synthetic_bytes() -> None:
    raw = b"abc"
    asset = MediaAsset(
        asset_id="x",
        filename="x.jpg",
        mime_type="image/jpeg",
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        content_base64="YWJj",
    )
    assert validate_media_asset(asset).asset_id == "x"
