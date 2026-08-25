# No More Buckets

No More Buckets is a Google All Things Agentic Hackathon submission: a bounded incident coordinator for plumbing leaks in small rental properties. It turns a tenant text, image, and/or voice note into an auditable workflow that contains the leak, creates a capped work order, dispatches a qualified vendor, recovers from vendor failure, coordinates access/ETA, verifies completion evidence, waits for tenant confirmation, and reopens a recurrence under warranty.

The product boundary is intentionally narrow. Gemini 3.5 Flash extracts schema-validated observable facts. Deterministic Python policies authorize safety actions, spending, access, vendor eligibility, retries, evidence gating, and closure. Tenant/vendor/media/invoice content is untrusted and never supplies policy instructions.

Telegram Bot API is the primary MVP messaging provider. It accepts tenant text, photos, and voice notes; sends containment and status updates; dispatches vendors with Accept/Decline buttons; and accepts typed `PRICE <amount>` / `ETA <minutes>` replies. Twilio remains an optional adapter only and is not on the critical path. Every Telegram user must send `/start` to the bot before receiving outbound messages.

## Quick start

The reproducible path uses the multi-stage container and deterministic adapters; it needs no cloud credentials and only synthetic data.

```powershell
docker build -t no-more-buckets:local .
docker run --rm -p 8080:8080 --env-file .env.example no-more-buckets:local
```

Open [http://localhost:8080](http://localhost:8080) and select **Run four-minute demo**. Demo time is compressed, but the same service/state machine logic records the same timeline and guardrails. A local source run is also possible with Python 3.12 and Node 22+. Use two terminals so the API and Vite console run together:

```powershell
# Terminal 1: credential-free deterministic API
$env:MESSAGING_PROVIDER = "local"
$env:STORAGE_BACKEND = "memory"
$env:FACTS_PROVIDER = "deterministic"
$env:ADK_ENABLED = "false"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend/requirements-dev.txt
python -m uvicorn app.main:app --app-dir backend --reload --port 8080

# Terminal 2: React/Vite console; open http://localhost:5173
cd frontend
npm install
npm run dev
```

## Workflow

```text
REPORTED → TRIAGED → CONTAINED → DISPATCHING → SCHEDULED → IN_PROGRESS
→ VERIFYING → PROVISIONALLY_RESOLVED → CLOSED
                         ↘ ESCALATED / CANCELLED / REOPENED
```

Important invariants:

- A report is idempotent by `Idempotency-Key` or a stable report key; event IDs are claimed before side effects. Notification, dispatch, Pub/Sub, Cloud Tasks, and timeline effects have deduplication keys.
- Vendor ranking filters active, insured, in-region plumbing vendors. Vendor A decline/timeout invokes the next eligible vendor. A late Vendor A acceptance is recorded as ignored once Vendor B is assigned.
- The synthetic Tampines property has a S$250 autonomous repair limit. A work order above the property limit becomes `ESCALATED` with a pending approval; safety escalation never silently expands spending authority.
- Missing, mismatched, low-confidence, wrong-vendor, out-of-scope, or over-limit completion evidence blocks closure.
- A closed incident recurs to `REOPENED` only while its warranty window is active; the original incident ID and timeline are preserved.
- Non-terminal incidents are scanned and resumed on service startup.

## Architecture

See [docs/architecture.md](docs/architecture.md), [docs/demo-script.md](docs/demo-script.md), and [docs/cloud-run.md](docs/cloud-run.md). The backend is under `backend/app/`, the React/Vite console under `frontend/`, and the deployment assets under `infra/` plus `Dockerfile`.

## Telegram setup

Create a bot with BotFather and keep the token only in a local, ignored `.env` file. The username is the BotFather username without `@`:

```powershell
Copy-Item .env.example .env
# Edit .env and fill these values; do not commit .env.
MESSAGING_PROVIDER=telegram
TELEGRAM_BOT_TOKEN=<BotFather token>
TELEGRAM_WEBHOOK_SECRET=<random secret>
TELEGRAM_BOT_USERNAME=<username without @>
```

The webhook secret is sent to Telegram when the webhook is registered and is checked on every request at `X-Telegram-Bot-Api-Secret-Token`. Generate one locally if needed:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

After the Cloud Run service is deployed, set `PUBLIC_BASE_URL` in `.env` or pass the URL explicitly. This command reads `.env`, calls Telegram `setWebhook`, then calls `getWebhookInfo` and fails if Telegram reports a different URL. It never prints the bot token or secret:

```powershell
$env:PYTHONPATH = "backend"
python scripts/register_telegram_webhook.py --base-url "https://YOUR-CLOUD-RUN-SERVICE.run.app"
```

Pair a synthetic tenant or vendor with a real Telegram chat through a one-time, 15-minute deep-link code. Pairing codes and the resulting chat IDs are data in Firestore; bot credentials remain configuration secrets:

```powershell
$pairing = Invoke-RestMethod -Method Post `
  -Uri "https://YOUR-CLOUD-RUN-SERVICE.run.app/api/telegram/pairing-codes" `
  -ContentType "application/json" `
  -Body '{"target_type":"vendor","target_id":"vendor-b"}'
$pairing.deep_link
```

Open the returned link in Telegram, or send `/start <code>` to the bot. The code is consumed exactly once and binds that chat to the selected record. Pair `tenant-demo-001` too if tenant updates should arrive in Telegram; the web UI can submit the tenant report without tenant pairing. For the simplest Cloud Run demo, pair `vendor-b`, submit from the web UI, let seeded Vendor A time out after 8/12 seconds, and show the Vendor B Telegram dispatch. The credential-free local adapter records the same timeout/fallback decision immediately so the local demo stays fast. The console displays a `Demo clock enabled` badge. Vendor A's late acceptance cannot replace Vendor B.

The local/demo adapter remains the credential-free default when `TELEGRAM_BOT_TOKEN` is empty. It uses the same workflow and idempotency logic, but records messages locally instead of calling Telegram. The seeded catalog contains only synthetic chat IDs; never put a bot token, webhook secret, or Gemini key in Firestore or source control.

## Cloud configuration

Copy `.env.example` to `.env` and set `MESSAGING_PROVIDER=telegram`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `GEMINI_API_KEY`, `FACTS_PROVIDER=gemini`, `STORAGE_BACKEND=firestore`, `GOOGLE_CLOUD_PROJECT`, `GCS_BUCKET`, and ADC credentials. Keep `GEMINI_MODEL=gemini-3.5-flash` and `GOOGLE_GENAI_USE_VERTEXAI=false` for the Gemini API path; there is no older-model fallback. The early smoke test and live contract test use the exact model and assert structured fields, not wording:

```powershell
$env:PYTHONPATH = "backend"
python -m app.gemini_smoke
$env:LIVE_GEMINI_TEST = "1"
python -m pytest backend/tests/contract -q
```

If `GEMINI_API_KEY` is missing or invalid, the deterministic adapter remains the local/demo path; the app does not silently substitute another Gemini model. Do not put real tenant/vendor data or production credentials in this demo. Never commit `.env`.

To seed the single synthetic property and the Telegram chat IDs into Firestore (chat IDs are data, not secrets):

```powershell
$env:PYTHONPATH = "backend"
python -m app.seed_data
```

## Verification

```powershell
python -m ruff check backend/app backend/tests
python -m mypy backend/app
python -m pytest backend/tests -q
cd frontend; npm run lint; npm test -- --run; npm run build
cd frontend; npx playwright test
docker build -t no-more-buckets:local .
```

If host dependencies are absent, run the commands in a Python/Node container or use the Docker build; no system package installation is required. `infra/smoke.ps1` checks `/api/health`, `/api/demo/seed`, and `/api/runtime`.
