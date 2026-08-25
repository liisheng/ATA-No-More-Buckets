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

Open [http://localhost:8080](http://localhost:8080). The primary screen is a live incident control room: it waits for a real backend report and polls persisted incident, communication, and media records without a refresh. **Replay deterministic scenario** is a secondary button for a repeatable end-to-end fallback/evidence demo; it does not represent the primary live experience. Demo time is compressed, but the same service/state machine logic records the same timeline and guardrails. A local source run is also possible with Python 3.12 and Node 22+. Use two terminals so the API and Vite console run together:

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

## Live control room

The primary console has three visible lanes for the selected incident:

- **Tenant conversation** shows tenant text, images, and voice notes, including a playable audio element and the persisted Gemini transcript.
- **Agent decisions / state** shows the current state, structured assessment, property-specific containment, scheduler contacts, and deterministic rule outcomes.
- **Vendor conversation** shows dispatch buttons/replies, fallback attempts, evidence photos, and invoice contacts.

The append-only audit timeline is separate from those conversations. Each communication record carries sender, recipient, channel, timestamp, provider message ID when available, and delivery state. A live Telegram send is marked `sent` after Telegram accepts the API request; local/demo contacts are marked `simulated`. The console does not automatically invent ETA, work-start, completion, or tenant-confirm events in the primary experience. Those arrive from Telegram or an explicit backend event; use the replay button only for the deterministic scenario.

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
TELEGRAM_DRAFT_EXPIRY_SECONDS=900
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

Open the returned link in Telegram, or send `/start <code>` to the bot. **Each Telegram user must send `/start` before the bot can send them messages.** The code is consumed exactly once and binds that chat to the selected record. Tenant conversation is deliberately explicit:

```text
/report
<send any number of text messages, photos, and voice notes>
Submit report       # inline button; creates exactly one incident
Add more            # inline button; keeps the same draft
Cancel              # inline button; discards it
```

Every update produces a draft summary. Drafts are persisted, expire after `TELEGRAM_DRAFT_EXPIRY_SECONDS`, and use Telegram `update_id` plus the draft idempotency key so duplicate delivery cannot create another incident. After `/start`, the paired tenant receives containment and live status updates. Gemini returns a schema-validated `ReportAssessment` with the faithful voice transcript when the Gemini provider is enabled; deterministic policy takes over for authorization.

A paired vendor receives a bounded work order with inline **Accept**/**Decline** buttons. After accepting, the vendor can send `PRICE 220 ETA 20`, then tap **Start job**. The bot requests an after-photo with this exact caption shape:

```text
COMPLETE
PRICE 220
SCOPE leak repair labor and replacement seal
```

The deterministic evidence gate checks the vendor photo, invoice scope, vendor identity, currency, confidence, and spending authority. After the compressed confirmation delay, the tenant receives **Dry now** or **Still leaking**. Dry now closes the original incident; Still leaking reopens that same incident only within its warranty window.

For the simplest Cloud Run demo, pair `vendor-b`, submit the tenant report from Telegram or the web UI, watch the visible Vendor A 8/12-second countdown expire, and show the Vendor B Telegram dispatch and reply. The local adapter uses the same timeout/fallback logic; the secondary replay advances the timeout explicitly so offline tests stay fast. The console displays a `Demo clock enabled` badge. Vendor A's late acceptance cannot replace Vendor B.

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
