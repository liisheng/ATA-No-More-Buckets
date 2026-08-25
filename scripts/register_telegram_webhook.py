"""Register and verify the Telegram webhook for a Cloud Run deployment.

The script reads ``.env`` from the repository root. It deliberately prints only
the public webhook URL and safe webhook status fields; bot tokens and secrets
are never included in output.

Usage:
    python scripts/register_telegram_webhook.py --base-url https://service-xyz.run.app
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters import TelegramBotAdapter  # noqa: E402
from app.config import Settings  # noqa: E402


def _validate_base_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base URL must be an absolute http(s) URL")
    return value.rstrip("/")


def register_webhook(
    settings: Settings, base_url: str, adapter: TelegramBotAdapter | None = None
) -> dict[str, object]:
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required in .env")
    if not settings.telegram_webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET is required in .env")
    if not settings.telegram_bot_username:
        raise ValueError("TELEGRAM_BOT_USERNAME is required in .env")
    webhook_url = f"{_validate_base_url(base_url)}/api/webhooks/telegram"
    telegram = adapter or TelegramBotAdapter(settings)
    telegram.set_webhook(webhook_url, settings.telegram_webhook_secret.get_secret_value())
    info = telegram.get_webhook_info()
    if info.get("url") != webhook_url:
        raise RuntimeError("Telegram webhook verification returned a different URL")
    return info


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        help="Cloud Run service URL; defaults to PUBLIC_BASE_URL from .env",
    )
    args = parser.parse_args(argv)
    env_file = ROOT / ".env"
    if not env_file.exists():
        raise SystemExit("Missing .env. Copy .env.example to .env and fill the Telegram placeholders.")
    settings = Settings(_env_file=env_file)  # type: ignore[call-arg]
    base_url = args.base_url or settings.public_base_url
    if not base_url:
        raise SystemExit("Provide --base-url or set PUBLIC_BASE_URL in .env")
    try:
        info = register_webhook(settings, base_url)
    except Exception:
        # Keep provider error bodies, tokens, and secrets out of terminal output.
        raise SystemExit("Telegram webhook registration or verification failed") from None
    pending = info.get("pending_update_count", 0)
    print(f"telegram_webhook_registered url={_validate_base_url(base_url)}/api/webhooks/telegram")
    print(f"telegram_webhook_verified pending_update_count={pending}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
