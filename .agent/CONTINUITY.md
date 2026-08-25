# ATA continuity briefing

## [PLANS]

- 2026-08-26T01:12+08:00 [USER] Supersedes the pause: resume No More Buckets setup, with Telegram bot creation, webhook registration, and tenant/vendor pairing as the next phase.
- 2026-08-26T01:29+08:00 [USER] Supersedes the prior setup phase: implement the coherent Telegram draft/vendor/completion/tenant conversation and leave the live control room ready for visual review.
- 2026-08-26T01:29+08:00 [CODE] Telegram conversation implementation and verification are complete; the remaining external check is Docker engine availability.
- 2026-08-26T01:00+08:00 [USER] No More Buckets is paused until the user chooses to resume; preserve the repository, working Cloud Run deployment, and current configuration without further implementation.
- 2026-08-24T05:11+08:00 [USER] Build “No More Buckets” as a greenfield, cloud-ready incident coordinator with a deterministic local/demo path, explicit safety state machine, adapters, tests, and submission documentation.
- 2026-08-24T05:11+08:00 [CODE] Repository is effectively empty; implementation will be split into `backend/`, `frontend/`, `infra/`, `docs/`, and `.agent/`.

## [DECISIONS]

- 2026-08-24T05:11+08:00 [ASSUMPTION] Local/demo execution defaults to deterministic adapters; Vertex AI remains an explicit opt-in provider configured with the exact `gemini-3.5-flash` model name.
- 2026-08-24T05:11+08:00 [ASSUMPTION] Firestore, Pub/Sub, Cloud Tasks, Cloud Storage, and Twilio are implemented behind interfaces with lazy cloud imports so tests do not require credentials.
- 2026-08-25T19:59+08:00 [USER] MVP messaging is Telegram Bot API; required configuration is exact Gemini 3.5 Flash, Gemini API mode by default, SGD, and a S$250 autonomous limit with compressed demo timers.
- 2026-08-25T19:59+08:00 [CODE] Telegram is the selected MVP provider; Twilio remains an optional adapter outside the critical path, and local/demo uses the same MessagingPort contract with deterministic delivery.
- 2026-08-25T23:41+08:00 [ASSUMPTION] Cloud deployment will use the Gemini Developer API key path (`GOOGLE_GENAI_USE_VERTEXAI=false`), a user-managed Cloud Run runtime service account, and a public Cloud Run URL required by Telegram; `CLOUD_TASKS_INVOKER_SERVICE_ACCOUNT` remains blank for this synthetic public demo.
- 2026-08-26T01:29+08:00 [CODE] Tenant Telegram reports now use persisted expiring drafts with text/photo/voice accumulation, inline Submit/Add more/Cancel controls, stable draft idempotency, and a submitted tombstone for repeated Submit taps.
- 2026-08-26T01:29+08:00 [CODE] Vendor Telegram callbacks now cover Accept/Decline/Start job, typed price/ETA, bounded completion captions, deterministic evidence gating, and tenant Dry now/Still leaking warranty controls.

## [PROGRESS]

- 2026-08-26T00:07+08:00 [USER] Google Cloud project `no-more-buckets` now has the `(default)` Firestore database created successfully in Native mode, Standard edition, `us-central1`, with free tier reported active.
- 2026-08-25T23:23+08:00 [TOOL] Visually verified the local app in the in-app browser: happy path reaches `CLOSED` with 20 timeline events and safety path reaches `ESCALATED` without dispatch. README local-source and Cloud Run demo timing instructions were corrected and pushed.
- 2026-08-25T23:18+08:00 [TOOL] Merged the remote GitHub placeholder README without discarding the project README, pushed `main` to `https://github.com/liisheng/ATA-No-More-Buckets`, and verified local `HEAD` matches `origin/main`.
- 2026-08-25T21:00+08:00 [CODE] Expanded the ignored local `.env` into the complete demo/Gemini/Telegram/GCP configuration: Gemini API facts are enabled, local memory remains the storage default, Telegram and cloud secrets stay blank/placeholders, and SGD policy plus compressed demo timings are explicit.
- 2026-08-25T20:40+08:00 [CODE] Initialized the workspace as a Git repository on `main` with a clean initial commit; `.env`, caches, virtualenvs, node_modules, and frontend build output remain ignored.
- 2026-08-24T05:11+08:00 [TOOL] Docker 29.5.3, Python 3.12.6, Node 24.18.0, and npm 11.15.0 are available; no Git repository metadata was present.
- 2026-08-24T05:44+08:00 [CODE] Backend, frontend, deployment assets, docs, deterministic adapters, cloud adapters, and tests are implemented; local smoke and browser demo pass.
- 2026-08-24T05:46+08:00 [CODE] Final local smoke, Pub/Sub invalid-incident handling, and three Playwright scenarios pass after the last backend hardening change.

## [DISCOVERIES]

- 2026-08-26T01:18+08:00 [TOOL] Secret Manager's Telegram token is valid for `ATANoMoreBucketsbot`, Cloud Run has that exact username, and Telegram points to the correct Cloud Run webhook. Delivery currently reports `403 Forbidden` because the local webhook secret does not match the deployed `telegram-webhook-secret`; bot tokens do match. One pending Telegram update remains queued.
- 2026-08-26T00:43+08:00 [TOOL] Cloud Run revision `no-more-buckets-00002-th9` failed before port binding because its cloud configuration had `STORAGE_BACKEND=firestore` but no `PUBLIC_BASE_URL`; logs show `GoogleCloudTasksQueue` raising the expected validation error. The previous bootstrap revision remains ready, and the service URL is `https://no-more-buckets-qzrxxtlpgq-uc.a.run.app`.
- 2026-08-26T00:45+08:00 [TOOL] The Gemini and two Telegram Secret Manager secrets exist with enabled versions, but none are bound to the failed Cloud Run service template yet; the recovery update must add `PUBLIC_BASE_URL` and all three secret bindings.
- 2026-08-24T05:11+08:00 [TOOL] No pre-existing source files, tests, or container workflow were found.
- 2026-08-24T05:28+08:00 [TOOL] Google ADK 1.14.1 depends on `google-cloud-storage<3`; storage was pinned to 2.19.0 so the reproducible environment resolves without weakening the Gemini model requirement.
- 2026-08-24T05:44+08:00 [TOOL] `docker build` remains unexecuted because Docker Desktop Linux engine is unavailable at `npipe:////./pipe/dockerDesktopLinuxEngine`.
- 2026-08-24T05:46+08:00 [TOOL] The Docker build was attempted again after fixing the multi-stage asset copy and remains blocked by the same unavailable engine; no image claim is made.
- 2026-08-25T11:30Z [TOOL] Review found production-blocking workflow gaps: Cloud Tasks uses invalid colon-delimited task IDs and a relative HTTP target; Pub/Sub delivery is neither provisioned nor decoded as a standard push envelope. Restart also dispatches past a pending vendor attempt, and an approved over-limit invoice is still rejected against the original cap.
- 2026-08-25T19:59+08:00 [CODE] Telegram webhook intake, secret validation, media download path, vendor buttons/typed replies, seeded chat IDs, explicit under-sink instructions, exact-model Gemini schema bridge, demo timing metadata, explicit approval authority, standard Pub/Sub dedupe, and Cloud Tasks absolute-target validation were added or hardened.
- 2026-08-25T20:04+08:00 [CODE] Added bounded transient vendor-provider retries through Cloud Tasks, a standalone synthetic catalog/Firestore seed CLI, local provider selection, and a visible demo-clock badge/timing strip in the console.
- 2026-08-25T20:07+08:00 [CODE] Added the reproducible `python -m app.gemini_smoke` command; it uses the exact model and redacts response/request details on failure.
- 2026-08-25T20:32+08:00 [CODE] Telegram deep-link pairing, `TELEGRAM_BOT_USERNAME` validation, fake API coverage, and the credential-safe webhook registration CLI were completed. Live demo mode now keeps Vendor A synthetic/timeout and dispatches the fallback to paired Vendor B through Telegram.
- 2026-08-25T20:32+08:00 [CODE] Firestore reference seeding is create-if-missing and startup reloads persisted reference records, preserving paired Telegram chat IDs across Cloud Run instance restarts.
- 2026-08-25T20:34+08:00 [CODE] Pairing-code expiry/consumption uses wall-clock UTC, while incident workflow timestamps retain the compressed demo clock; the documented 15-minute pairing window therefore remains real.
- 2026-08-25T20:36+08:00 [CODE] Live Telegram demo `timeout` now returns a pending Vendor A attempt so the existing 8/12-second vendor-timeout task, rather than immediate dispatch, triggers Vendor B fallback.
- 2026-08-25T23:58+08:00 [CODE] Replaced the primary replay-centric console with a polling live incident control room. The secondary button is now `Replay deterministic scenario`; the primary waits for persisted backend incidents.
- 2026-08-25T23:58+08:00 [CODE] Added persisted communication records for tenant/agent/vendor/scheduler contacts, stable upsert IDs, incident-scoped media reads, image/audio descriptors, and structured `ReportAssessment` transcript/conflict/missing-information fields.
- 2026-08-25T23:41+08:00 [TOOL] Cloud-backed startup requires `PUBLIC_BASE_URL` because the Cloud Tasks adapter constructs an absolute callback URL; deployment therefore needs a bootstrap local-adapter revision, followed by a cloud-adapter revision after reading the Cloud Run service URL.
- 2026-08-25T23:41+08:00 [TOOL] Pub/Sub publishing is part of the cloud adapter, but end-to-end push delivery to `/api/events/pubsub` additionally requires a push subscription; workflow progression itself uses Cloud Tasks and does not depend on that subscription.

## [OUTCOMES]

- 2026-08-26T01:18+08:00 [USER] Telegram webhook re-registration succeeded for `ATANoMoreBucketsbot` at the Cloud Run `/api/webhooks/telegram` endpoint using the deployed secret; verification reports zero pending updates. Tenant and Vendor B chat pairing is next.
- 2026-08-26T01:12+08:00 [TOOL] Using the deployed `gemini-api-key` secret, the exact-model live smoke passed with all required structured fields and the opt-in live Gemini contract test passed (`live_smoke_exit=0`, `live_contract_exit=0`).
- 2026-08-26T00:54+08:00 [TOOL] The local `.env` Gemini key is accepted but returns `429 RESOURCE_EXHAUSTED` because its AI Studio prepayment credits are depleted. It does not match the `gemini-api-key` Secret Manager value; a redacted live request using the Cloud secret succeeds against exact model `gemini-3.5-flash`. Cloud Run revision `no-more-buckets-00005-q5n` is ready with the public base URL and all three secret references configured.
- 2026-08-24T05:44+08:00 [CODE] Ruff, mypy, 20 backend tests plus one skipped live Vertex contract test, frontend lint/Vitest/build, npm production audit, local smoke, and three Playwright tests pass. Docker build is pending Docker daemon availability.
- 2026-08-24T05:46+08:00 [CODE] Final verification remains green: 20 backend tests plus one skipped opt-in Vertex test, frontend lint/Vitest/build, npm production audit, local smoke, and 3 Playwright tests; Docker is the only pending external runtime check.
- 2026-08-25T19:59+08:00 [TOOL] Exact-model live Gemini smoke reached `gemini-3.5-flash`; the SDK schema bridge required removal of Pydantic `additionalProperties`, then the configured key was rejected as `API_KEY_INVALID`. No older model fallback was used.
- 2026-08-25T19:59+08:00 [CODE] Backend tests currently pass (26 passed, 1 skipped); Ruff and mypy pass after the Telegram/spec update. Frontend, Playwright, Docker, and final smoke still require this turn's verification.
- 2026-08-25T20:04+08:00 [TOOL] Backend tests pass (28 passed, 1 skipped), Ruff/mypy pass, frontend lint/Vitest/build pass, three Playwright scenarios pass, and local HTTP smoke reports a scheduled demo incident with the compressed timers. Docker build remains blocked by the unavailable Docker Desktop Linux engine.
- 2026-08-25T20:07+08:00 [TOOL] The reproducible live smoke command reached Gemini but failed with `ClientError` from the configured key; schema compatibility was already verified by the prior API-key-invalid response, and no model fallback was attempted. Final backend result is 29 passed, 1 skipped.
- 2026-08-25T20:07+08:00 [TOOL] Final frontend lint, Vitest, TypeScript/Vite build, and three Playwright scenarios pass. Docker build was attempted and remains blocked at the Docker API pipe because the Linux engine is unavailable.
- 2026-08-25T20:36+08:00 [TOOL] Local backend verification is 36 passed, 1 skipped; Ruff and mypy pass; frontend lint, Vitest, TypeScript/Vite build, and 3 Playwright scenarios pass; local HTTP smoke passes. Docker build remains blocked by the unavailable Docker Desktop Linux engine.
- 2026-08-25T23:58+08:00 [TOOL] The primary flow no longer fabricates ETA, work-start, completion, or tenant-confirm events. A delayed confirmation task sends a reminder; only a tenant confirmation response closes the incident.
- 2026-08-25T23:58+08:00 [TOOL] Backend verification is 40 passed, 1 skipped; Ruff/mypy pass; frontend lint, Vitest, TypeScript/Vite build, and 3 Playwright scenarios pass. The in-app browser visually verified the live waiting state, backend-driven three-lane incident view, persisted contacts, transcript/audio player, and tenant/completion media surfaces.
- 2026-08-25T20:32+08:00 [TOOL] The exact-model live Gemini smoke reports `model=gemini-3.5-flash` and fails with a redacted `ClientError`; no older model fallback is used.
- 2026-08-26T01:29+08:00 [TOOL] Backend verification is 48 passed, 1 skipped; Ruff and mypy pass; frontend lint, Vitest, TypeScript/Vite build, and 3 Playwright scenarios pass. Local HTTP smoke reports `No More Buckets`, exact model `gemini-3.5-flash`, deterministic/memory/local event adapters, and demo clock enabled.
- 2026-08-26T01:29+08:00 [TOOL] Local demo Vendor A timeout is scheduled through the same task logic and fires automatically in demo runtime; urgent countdown is 8 seconds and routine countdown is 12 seconds. Docker build remains blocked by the unavailable Docker Desktop Linux engine pipe.
