# Submission polish

## The one-sentence pitch

No More Buckets turns a tenant message, photo, or voice note into a safely coordinated repair, from assessment and vendor dispatch through evidence and tenant confirmation.

## What the four-minute demo proves

The primary console is a live three-lane control room driven by Telegram and persisted backend events: multimodal report → schema-validated facts → property-specific containment → bounded work order → Vendor A failure → Vendor B fallback → ETA/access → completion photo and invoice gate → delayed tenant confirmation → final state. The proof strip reports the active deployment, model, source of truth, and timing mode. A secondary deterministic replay is available only in local development.

## Why the agentic design is trustworthy

The model is useful where language and media are ambiguous: it extracts observations. The deterministic layer is authoritative where safety and money matter. The state machine prevents impossible transitions, durable event claims prevent duplicate side effects, and every decision is represented by a rule ID instead of hidden reasoning.

## Deliberate scope

This submission coordinates one narrow incident class: plumbing leaks in small rentals. It does not add rent collection, maintenance catalogs, inspections, tenant screening, or decorative multi-agent roles. Synthetic data is used throughout.
