"""Opt-in live Gemini 3.5 Flash structured-output smoke test."""

from .adapters import VertexGeminiFactExtractor, build_demo_media
from .config import get_settings


def main() -> None:
    settings = get_settings()
    if not settings.gemini_api_key:
        print("LIVE_GEMINI_SMOKE_SKIPPED: GEMINI_API_KEY is not configured")
        return
    if settings.gemini_model != "gemini-3.5-flash":
        raise SystemExit("GEMINI_MODEL must be exactly gemini-3.5-flash")
    try:
        facts = VertexGeminiFactExtractor(settings).extract(
            "Water is dripping under the kitchen sink; the outlet is dry.",
            "I can reach the shutoff.",
            [build_demo_media()],
        )
    except Exception as exc:
        # Do not print response bodies or credential-bearing request details.
        raise SystemExit(
            f"LIVE_GEMINI_SMOKE_FAILED model=gemini-3.5-flash error={type(exc).__name__}"
        ) from None
    required = {"issue_type", "severity", "water_visible", "source_confidence"}
    if not required.issubset(facts.model_dump()):
        raise SystemExit("LIVE_GEMINI_SMOKE_FAILED: required structured fields are missing")
    print(
        "LIVE_GEMINI_SMOKE_OK model=gemini-3.5-flash "
        "structured_fields=issue_type,severity,water_visible,source_confidence"
    )


if __name__ == "__main__":
    main()
