# Four-minute demo script

The console compresses timing while preserving the production workflow logic. The top bar visibly says **DEMO CLOCK ENABLED**. The configured demo timers are Vendor A urgent timeout 8s, routine timeout 12s, tenant confirmation 15s, and warranty recurrence 30s.

1. Open the console and leave the primary screen waiting. It visibly says **LIVE BACKEND FEED** and **DEMO CLOCK ENABLED** when demo mode is active; there is no automatic workflow replay.
2. Send `/start` to the Telegram bot from the paired synthetic tenant, then send a text report with a photo and/or voice note. The tenant lane updates without a refresh and shows sender, recipient, Telegram channel, time, `sent`/`received` state, image, audio player, and transcript.
3. Point at the agent lane: Gemini’s schema-validated `ReportAssessment` is shown beside the deterministic state, property-specific under-sink containment, S$250 cap, and persisted scheduler/contact records. The timeline separately shows `REPORTED → TRIAGED → CONTAINED` and rule IDs.
4. Show the visible Vendor A 8-second urgent (or 12-second routine) countdown, timeout, and autonomous Vendor B fallback. Explain that a late Vendor A acceptance cannot replace the assigned Vendor B. In Telegram, Vendor B receives Accept/Decline buttons, replies `PRICE 220 ETA 20`, and taps **Start job**.
5. Send the after-photo with the exact `COMPLETE / PRICE / SCOPE` caption. Show the completion image, invoice scope gate, delayed confirmation, then tap **Dry now** or **Still leaking**. The primary console does not fabricate ETA, work-start, completion, or tenant-confirm contacts; each appears only after the real Telegram/backend event.
6. Point at the proof strip for Cloud Run/Gemini/Firestore adapter execution. In local mode, it explicitly says local container/deterministic/memory; in GCP mode, the same fields show Cloud Run/Gemini 3.5 Flash/Firestore and Pub/Sub + Cloud Tasks.
7. If the live Telegram path is unavailable, use the secondary **Replay deterministic scenario** button. It retains the repeatable four-minute storyline for judging and regression checks, but it is clearly labeled as a replay rather than the live control room.

Exception path to rehearse: submit text containing `electrical` or a quote above the property cap. The service produces a narrow `ESCALATED` timeline entry and does not dispatch.
