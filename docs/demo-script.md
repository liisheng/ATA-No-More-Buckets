# Four-minute demo script

The console compresses timing while preserving the production workflow logic. The top bar visibly says **DEMO CLOCK ENABLED**. The configured demo timers are Vendor A urgent timeout 8s, routine timeout 12s, tenant confirmation 15s, and warranty recurrence 30s.

1. Press **Run four-minute demo**. The tenant report visibly includes Telegram-style text, an attached photo, and a voice transcript. The assessment panel shows schema-validated facts and the exact `gemini-3.5-flash` configured model badge (or deterministic provider in local mode).
2. Point at the containment message: it is property-specific and is sent before dispatch. The persisted timeline shows `REPORTED → TRIAGED → CONTAINED` plus the containment rule ID.
3. Show Vendor A timing out/declining. The timeline records the failure and autonomous Vendor B fallback. Explain that a late Vendor A acceptance is ignored because the assigned vendor wins the race. In Telegram, this same dispatch uses Accept/Decline buttons and typed `PRICE`/`ETA` replies.
4. Show the ETA notification and vendor check-in moving the incident to `IN_PROGRESS`.
5. Show the completion photo and invoice assessment. The evidence gate records photo confidence, invoice scope, and spending check before `PROVISIONALLY_RESOLVED`.
6. Show the delayed tenant confirmation task and the final `CLOSED` state. Mention that a recurrence inside the warranty period moves the original incident to `REOPENED`.
7. Point at the proof strip for Cloud Run/Gemini/Firestore adapter execution. In local mode, it explicitly says local container/deterministic/memory; in GCP mode, the same fields show Cloud Run/Gemini 3.5 Flash/Firestore and Pub/Sub + Cloud Tasks.

Exception path to rehearse: submit text containing `electrical` or a quote above the property cap. The service produces a narrow `ESCALATED` timeline entry and does not dispatch.
