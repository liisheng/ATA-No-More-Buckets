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
