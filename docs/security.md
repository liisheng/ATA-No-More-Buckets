# Safety and data handling

- Tenant, vendor, media, and invoice fields are bounded, validated, and treated as untrusted. Prompts explicitly delimit them and reject embedded instructions.
- The Gemini adapter receives a response schema for observable facts. It cannot authorize spending, safety, dispatch, or closure.
- Safety, access, spend, vendor eligibility, fallback, evidence, and warranty decisions are deterministic functions with rule IDs.
- Log fields are limited to incident ID, event ID, state transition, rule ID, and tool outcome. Do not log raw tenant text, transcripts, media, invoices, or model reasoning.
- The demo uses synthetic data only. Cloud Storage paths and Firestore documents use incident/asset IDs rather than user-provided paths.
- Provider adapters use idempotency keys and durable event claims before side effects. Late provider events are recorded but cannot overwrite an assigned vendor.
