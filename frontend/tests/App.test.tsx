import { render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import App from "../src/App";

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({
    environment: "demo", deployment: "local_container", facts_provider: "deterministic",
    facts_model: "gemini-3.5-flash", storage_backend: "memory", eventing: "local_event_bus+local_tasks", synthetic_data_only: true,
  }) }));
});

it("renders the demo control room and safety contract", async () => {
  render(<App />);
  await screen.findByText(/local_container/i);
  expect(screen.getByText("no more buckets")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /run four-minute demo/i })).toBeInTheDocument();
  expect(screen.getByText(/Facts first. Policy second./i)).toBeInTheDocument();
});
