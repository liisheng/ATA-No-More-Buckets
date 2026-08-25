import os

import pytest

from app.adapters import VertexGeminiFactExtractor, build_demo_media
from app.config import Settings


@pytest.mark.skipif(os.getenv("LIVE_GEMINI_TEST") != "1", reason="opt-in live Gemini contract test")
def test_gemini_returns_schema_validated_observable_facts() -> None:
    settings = Settings(facts_provider="gemini", gemini_api_key=os.environ["GEMINI_API_KEY"])
    assert settings.gemini_model == "gemini-3.5-flash"
    facts = VertexGeminiFactExtractor(settings).extract(
        "Water is dripping below the kitchen sink; the outlet is dry.",
        "I can reach the shutoff.",
        [build_demo_media()],
    )
    assert facts.issue_type.value in {"leak", "flood", "drain", "unknown"}
    assert 0 <= facts.source_confidence <= 1
