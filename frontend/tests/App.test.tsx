import { act, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import App from "../src/App";
import { api } from "../src/api";

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

it("normalizes the privacy 404 from draft inspection to an empty list", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
    ok: false,
    status: 404,
    text: async () => '{"detail":"draft inspection is disabled in live mode"}',
  }));

  await expect(api.drafts()).resolves.toEqual([]);
});

it("renders the newest live incident and its communications and media when drafts are private", async () => {
  const communication = {
    communication_id: "comm-1", incident_id: "inc-live", sender_role: "tenant" as const, sender_id: "tenant-1",
    recipient_role: "agent" as const, recipient_id: "agent", channel: "telegram", direction: "inbound" as const,
    message_type: "image" as const, text: "Leak photo", media_ids: ["media-1"], delivery_status: "received" as const,
    timestamp: "2026-08-28T10:00:00Z",
  };
  const media = { media_id: "media-1", filename: "leak.png", mime_type: "image/png", size_bytes: 4, source: "tenant" as const, url: "/media/leak.png" };
  const incident = {
    incident_id: "inc-live", property_id: "unit-1", tenant_id: "tenant-1", status: "DISPATCHING" as const,
    report_text: "Water is dripping under the sink", media_ids: ["media-1"], vendor_attempts: [],
    created_at: "2026-08-28T10:00:00Z", updated_at: "2026-08-28T10:00:01Z", timeline: [],
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [incident] };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => '{"detail":"draft inspection is disabled in live mode"}' };
    if (path.endsWith("/api/incidents/inc-live")) return { ok: true, json: async () => incident };
    if (path.endsWith("/communications")) return { ok: true, json: async () => [communication] };
    if (path.endsWith("/media")) return { ok: true, json: async () => [media] };
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText(/active incident · inc-live/i);
  expect(screen.getByText("Leak photo")).toBeInTheDocument();
  expect(screen.getByAltText(/leak\.png/i)).toBeInTheDocument();
  expect(screen.queryByText(/draft inspection is disabled/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/waiting for a real tenant report/i)).not.toBeInTheDocument();
});

it("updates the incident and media when communications polling fails", async () => {
  const incident = {
    incident_id: "inc-communications-failure", property_id: "unit-1", tenant_id: "tenant-1", status: "DISPATCHING" as const,
    report_text: "Current incident status", media_ids: ["media-1"], vendor_attempts: [],
    created_at: "2026-08-28T10:00:00Z", updated_at: "2026-08-28T10:00:01Z", timeline: [],
  };
  const media = { media_id: "media-1", filename: "current.png", mime_type: "image/png", size_bytes: 4, source: "tenant" as const, url: "/media/current.png" };
  const fetchMock = vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [incident] };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => "private" };
    if (path.endsWith("/api/incidents/inc-communications-failure")) return { ok: true, json: async () => incident };
    if (path.endsWith("/communications")) return { ok: false, status: 503, text: async () => "communications unavailable" };
    if (path.endsWith("/media")) return { ok: true, json: async () => [media] };
    return { ok: true, json: async () => [] };
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<App />);
  await screen.findByText("Current incident status");
  expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/media"))).toBe(true);
  expect(screen.getByText(/Communications refresh failed: communications unavailable/i)).toBeInTheDocument();
});

it("updates the incident and communications when media polling fails", async () => {
  const incident = {
    incident_id: "inc-media-failure", property_id: "unit-1", tenant_id: "tenant-1", status: "DISPATCHING" as const,
    report_text: "Current incident status", media_ids: [], vendor_attempts: [],
    created_at: "2026-08-28T10:00:00Z", updated_at: "2026-08-28T10:00:01Z", timeline: [],
  };
  const communication = {
    communication_id: "comm-1", incident_id: "inc-media-failure", sender_role: "tenant" as const, sender_id: "tenant-1",
    recipient_role: "agent" as const, recipient_id: "agent", channel: "telegram", direction: "inbound" as const,
    message_type: "text" as const, text: "Communication is current", media_ids: [], delivery_status: "received" as const,
    timestamp: "2026-08-28T10:00:00Z",
  };
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [incident] };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => "private" };
    if (path.endsWith("/api/incidents/inc-media-failure")) return { ok: true, json: async () => incident };
    if (path.endsWith("/communications")) return { ok: true, json: async () => [communication] };
    if (path.endsWith("/media")) return { ok: false, status: 503, text: async () => "media unavailable" };
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText("Current incident status");
  expect(screen.getByText("Communication is current")).toBeInTheDocument();
  expect(screen.getByText(/Media refresh failed: media unavailable/i)).toBeInTheDocument();
});

it("renders incident details before a deferred media request completes", async () => {
  const incident = {
    incident_id: "inc-deferred-media", property_id: "unit-1", tenant_id: "tenant-1", status: "DISPATCHING" as const,
    report_text: "Immediate incident status", media_ids: [], vendor_attempts: [],
    created_at: "2026-08-28T10:00:00Z", updated_at: "2026-08-28T10:00:01Z", timeline: [],
  };
  const communication = {
    communication_id: "comm-1", incident_id: "inc-deferred-media", sender_role: "tenant" as const, sender_id: "tenant-1",
    recipient_role: "agent" as const, recipient_id: "agent", channel: "telegram", direction: "inbound" as const,
    message_type: "text" as const, text: "Immediate communication", media_ids: [], delivery_status: "received" as const,
    timestamp: "2026-08-28T10:00:00Z",
  };
  let releaseMedia!: () => void;
  const deferredMedia = new Promise<Response>((resolve) => { releaseMedia = () => resolve({ ok: true, json: async () => [] } as Response); });
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => [incident] };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => "private" };
    if (path.endsWith("/api/incidents/inc-deferred-media")) return { ok: true, json: async () => incident };
    if (path.endsWith("/communications")) return { ok: true, json: async () => [communication] };
    if (path.endsWith("/media")) return deferredMedia;
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText("Immediate incident status");
  expect(screen.getByText("Immediate communication")).toBeInTheDocument();
  expect(screen.queryByText(/Media refresh failed/i)).not.toBeInTheDocument();
  await act(async () => { releaseMedia(); });
});

it("discards out-of-order results from an older polling cycle", async () => {
  const oldIncident = {
    incident_id: "inc-old-poll", property_id: "unit-1", tenant_id: "tenant-1", status: "DISPATCHING" as const,
    report_text: "Old incident status", media_ids: [], vendor_attempts: [],
    created_at: "2026-08-28T10:00:00Z", updated_at: "2026-08-28T10:00:01Z", timeline: [],
  };
  const newIncident = { ...oldIncident, incident_id: "inc-new-poll", report_text: "New incident status", updated_at: "2026-08-28T10:00:02Z" };
  const communication = {
    communication_id: "comm-new", incident_id: "inc-new-poll", sender_role: "tenant" as const, sender_id: "tenant-1",
    recipient_role: "agent" as const, recipient_id: "agent", channel: "telegram", direction: "inbound" as const,
    message_type: "text" as const, text: "New communication", media_ids: [], delivery_status: "received" as const,
    timestamp: "2026-08-28T10:00:02Z",
  };
  let incidentsPoll = 0;
  let rejectOldCommunications!: (reason: Error) => void;
  const oldCommunications = new Promise<Response>((_, reject) => { rejectOldCommunications = reject; });
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: true, json: async () => (++incidentsPoll === 1 ? [oldIncident] : [newIncident]) };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => "private" };
    if (path.endsWith("/api/incidents/inc-old-poll")) return new Promise<Response>(() => undefined);
    if (path.endsWith("/api/incidents/inc-new-poll")) return { ok: true, json: async () => newIncident };
    if (path.endsWith("/inc-old-poll/communications")) return oldCommunications;
    if (path.endsWith("/inc-new-poll/communications")) return { ok: true, json: async () => [communication] };
    if (path.endsWith("/media")) return { ok: true, json: async () => [] };
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText("New incident status", {}, { timeout: 2500 });
  expect(screen.getByText("New communication")).toBeInTheDocument();
  rejectOldCommunications(new Error("old communications failure"));
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(screen.queryByText(/old communications failure/i)).not.toBeInTheDocument();
  expect(screen.getByText("New incident status")).toBeInTheDocument();
});

it("surfaces meaningful incident polling failures", async () => {
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path.endsWith("/api/runtime")) return { ok: true, json: async () => ({ environment: "live", deployment: "cloud_run", facts_provider: "gemini", facts_model: "gemini-3.5-flash", storage_backend: "firestore", eventing: "cloud_tasks", messaging_provider: "telegram", demo_clock_enabled: false, demo_timings_seconds: {}, synthetic_data_only: false }) };
    if (path.endsWith("/api/incidents")) return { ok: false, status: 503, text: async () => "incident service unavailable" };
    if (path.endsWith("/api/drafts")) return { ok: false, status: 404, text: async () => '{"detail":"draft inspection is disabled in live mode"}' };
    return { ok: true, json: async () => [] };
  }));

  render(<App />);
  await screen.findByText("incident service unavailable");
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
  await screen.findByText(/Vendor B waiting for response · (?:10:00|09:5\d) remaining/);
  expect(screen.queryByText(/Vendor A timeout in 0s/)).not.toBeInTheDocument();
  await screen.findByText(/Vendor B waiting for response · 09:5\d remaining/, {}, { timeout: 2500 });

  currentIncident = {
    ...pendingIncident,
    vendor_attempts: pendingIncident.vendor_attempts.map((attempt) => attempt.vendor_id === "vendor-b" ? { ...attempt, attempt_id: "attempt-b-fallback", deadline_at: undefined } : attempt),
    updated_at: "2026-08-28T10:00:15Z",
  };
  await screen.findByText(/Vendor B waiting for response · 00:\d\d remaining/);

  currentIncident = {
    ...pendingIncident,
    status: "SCHEDULED",
    vendor_attempts: pendingIncident.vendor_attempts.map((attempt) => attempt.vendor_id === "vendor-b" ? { ...attempt, outcome: "accepted" } : attempt),
    updated_at: "2026-08-28T10:00:14Z",
    timeline: [...pendingIncident.timeline, { event_id: "accepted-b", at: "2026-08-28T10:00:14Z", kind: "vendor_dispatch_outcome", metadata: { vendor_id: "vendor-b", outcome: "accept" } }],
  };
  await screen.findByText("Vendor B accepted", {}, { timeout: 2500 });
});
