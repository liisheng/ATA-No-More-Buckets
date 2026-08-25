import { expect, test } from "@playwright/test";

test("runs the happy-path demo through closure", async ({ page }) => {
  await page.request.post("/api/demo/reset");
  await page.goto("/");
  await page.getByRole("button", { name: /replay deterministic scenario/i }).click();
  await expect(page.getByText("CLOSED").first()).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/vendor b fallback/i)).toBeVisible();
  await expect(page.getByText(/completion evidence assessed/i)).toBeVisible();
});

test("renders the persisted timeline surface", async ({ page }) => {
  await page.request.post("/api/demo/reset");
  await page.goto("/");
  await expect(page.getByText(/waiting for a real tenant report/i)).toBeVisible();
  await expect(page.getByText(/live backend feed/i)).toBeVisible();
  await expect(page.getByText(/telegram primary/i)).toBeVisible();
});

test("blocks an unsafe electrical exception", async ({ page }) => {
  await page.request.post("/api/demo/reset");
  await page.goto("/");
  await page.getByRole("button", { name: /try safety exception/i }).click();
  await expect(page.getByText("ESCALATED", { exact: true }).first()).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("SAFETY_OCCUPANT_DANGER").first()).toBeVisible();
});
