import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import App from "../src/App";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    const body = path.endsWith("/api/runtime")
      ? { environment: "demo", deployment: "local_container", facts_provider: "deterministic", facts_model: "gemini-3.5-flash", storage_backend: "memory", eventing: "local_event_bus+local_tasks", messaging_provider: "local_demo", demo_clock_enabled: true, demo_timings_seconds: {}, synthetic_data_only: true }
      : [];
    return { ok: true, json: async () => body };
  }));
});

it("renders the live control room and safety contract", async () => {
  render(<App />);
  await screen.findByText(/local_container/i);
  expect(screen.getByText("no more buckets")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /replay deterministic scenario/i })).toBeInTheDocument();
  expect(screen.getByText(/waiting for a real tenant report/i)).toBeInTheDocument();
  expect(screen.getByText(/live backend feed/i)).toBeInTheDocument();
});

it("renders an incoming multimodal draft before submission", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) {
      return { ok: true, json: async () => ({ environment: "demo", deployment: "local_container", facts_provider: "deterministic", facts_model: "gemini-3.5-flash", storage_backend: "memory", eventing: "local", messaging_provider: "local_demo", demo_clock_enabled: true, demo_timings_seconds: {}, synthetic_data_only: true }) };
    }
    if (path.endsWith("/api/drafts")) {
      return { ok: true, json: async () => [{ draft_id: "draft-1", tenant_id: "tenant-1", property_id: "unit-1", text_parts: ["Water is dripping under the sink"], media: [{ media_id: "photo-1", filename: "leak.png", mime_type: "image/png", size_bytes: 3, source: "tenant", url: "/api/drafts/draft-1/media/photo-1" }, { media_id: "video-1", filename: "leak.mp4", mime_type: "video/mp4", size_bytes: 3, source: "tenant", url: "/api/drafts/draft-1/media/video-1" }, { media_id: "voice-1", filename: "voice.ogg", mime_type: "audio/ogg", size_bytes: 3, source: "tenant", url: "/api/drafts/draft-1/media/voice-1", duration_seconds: 4 }], communications: [], created_at: "2026-08-26T12:00:00Z", updated_at: "2026-08-26T12:00:01Z", expires_at: "2026-08-26T12:15:00Z" }] };
    }
    return { ok: true, json: async () => [] };
  }));
  render(<App />);
  await screen.findByText(/incoming report draft/i);
  expect(screen.getByText("videos")).toBeInTheDocument();
  expect(document.querySelector("video")).not.toBeNull();
  expect(document.querySelector("audio")).not.toBeNull();
  expect(screen.getByText(/transcript pending/i)).toBeInTheDocument();
});

it("focuses the newest active incident when timestamps differ", async () => {
  const older = { incident_id: "inc-old", property_id: "unit-1", tenant_id: "tenant-1", status: "CLOSED", report_text: "Older report", media_ids: [], vendor_attempts: [], created_at: "2026-08-26T12:00:00Z", updated_at: "2026-08-26T12:00:01Z", timeline: [] };
  const newer = { ...older, incident_id: "inc-new", status: "DISPATCHING", report_text: "Newest report", updated_at: "2026-08-26T12:00:02Z" };
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) {
      return { ok: true, json: async () => ({ environment: "demo", deployment: "local_container", facts_provider: "deterministic", facts_model: "gemini-3.5-flash", storage_backend: "memory", eventing: "local", messaging_provider: "local_demo", demo_clock_enabled: true, demo_timings_seconds: {}, synthetic_data_only: true }) };
    }
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [older, newer] };
    if (path.endsWith("/api/drafts")) return { ok: true, json: async () => [] };
    if (path.endsWith("/api/incidents/inc-new")) return { ok: true, json: async () => newer };
    if (path.includes("/api/incidents/inc-new/")) return { ok: true, json: async () => [] };
    return { ok: true, json: async () => [] };
  }));
  render(<App />);
  await screen.findByText(/active incident · inc-new/i);
  expect(screen.getByText("Newest report")).toBeInTheDocument();
  expect(screen.queryByText("Older report")).not.toBeInTheDocument();
});

it("keeps vendor countdowns and outcomes attached to the matching vendor attempt", async () => {
  const pendingIncident = {
    incident_id: "inc-vendor-countdown",
    property_id: "unit-1",
    tenant_id: "tenant-1",
    status: "DISPATCHING" as const,
    report_text: "Water is dripping under the sink",
    media_ids: [],
    vendor_attempts: [
      { vendor_id: "vendor-a", outcome: "timed_out", attempt_id: "attempt-a", event_id: "event-a", at: "2026-08-28T10:00:00Z" },
      { vendor_id: "vendor-b", outcome: "pending", attempt_id: "attempt-b", event_id: "event-b", at: "2099-01-01T00:00:00Z", deadline_at: "2099-01-01T00:10:00Z" },
    ],
    created_at: "2026-08-28T10:00:00Z",
    updated_at: "2026-08-28T10:00:12Z",
    timeline: [
      { event_id: "timeout-a", at: "2026-08-28T10:00:08Z", kind: "vendor_timeout_scheduled", metadata: { vendor_id: "vendor-a", timeout_seconds: 8, deadline_at: "2026-08-28T10:00:08Z" } },
      { event_id: "timeout-b-old", at: "2099-01-01T00:00:01Z", kind: "vendor_timeout_scheduled", metadata: { vendor_id: "vendor-b", timeout_seconds: 30, deadline_at: "2099-01-01T00:00:30Z" } },
      { event_id: "timeout-b-new", at: "2099-01-01T00:00:02Z", kind: "vendor_timeout_scheduled", metadata: { vendor_id: "vendor-b", timeout_seconds: 30, deadline_at: "2099-01-01T00:00:30Z" } },
    ],
  };
  let currentIncident = pendingIncident;
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "demo", deployment: "local_container", facts_provider: "deterministic", facts_model: "gemini-3.5-flash", storage_backend: "memory", eventing: "local", messaging_provider: "local_demo", demo_clock_enabled: true, demo_timings_seconds: {}, synthetic_data_only: true }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [currentIncident] };
    if (path.endsWith("/api/drafts")) return { ok: true, json: async () => [] };
    if (path.endsWith("/api/incidents/inc-vendor-countdown")) return { ok: true, json: async () => currentIncident };
    if (path.includes("/api/incidents/inc-vendor-countdown/")) return { ok: true, json: async () => [] };
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText("Vendor A timed out / failed");
  expect(screen.getByText(/Vendor B waiting for response · (?:10:00|09:5\d) remaining/)).toBeInTheDocument();
  expect(screen.queryByText(/Vendor A timeout in 0s/)).not.toBeInTheDocument();
  await screen.findByText(/Vendor B waiting for response · 09:5\d remaining/, {}, { timeout: 2500 });

  currentIncident = {
    ...pendingIncident,
    vendor_attempts: pendingIncident.vendor_attempts.map((attempt) => attempt.vendor_id === "vendor-b" ? { ...attempt, attempt_id: "attempt-b-fallback", deadline_at: undefined } : attempt),
    updated_at: "2026-08-28T10:00:15Z",
  };
  await screen.findByText(/Vendor B waiting for response · 00:30 remaining/);

  currentIncident = {
    ...pendingIncident,
    status: "SCHEDULED",
    vendor_attempts: pendingIncident.vendor_attempts.map((attempt) => attempt.vendor_id === "vendor-b" ? { ...attempt, outcome: "accepted" } : attempt),
    updated_at: "2026-08-28T10:00:14Z",
    timeline: [...pendingIncident.timeline, { event_id: "accepted-b", at: "2026-08-28T10:00:14Z", kind: "vendor_dispatch_outcome", metadata: { vendor_id: "vendor-b", outcome: "accept" } }],
  };
  await screen.findByText("Vendor B accepted", {}, { timeout: 2500 });
});
