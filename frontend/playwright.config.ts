import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  use: { baseURL: "http://127.0.0.1:8080", trace: "retain-on-failure" },
  webServer: { command: "python -m uvicorn app.main:app --app-dir ../backend --host 127.0.0.1 --port 8080", port: 8080, reuseExistingServer: true },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
