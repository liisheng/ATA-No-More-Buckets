import { expect, test } from "@playwright/test";

test("runs the happy-path demo through closure", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /run four-minute demo/i }).click();
  await expect(page.getByText("CLOSED")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/vendor b fallback/i)).toBeVisible();
  await expect(page.getByText(/completion evidence assessed/i)).toBeVisible();
});

test("renders the persisted timeline surface", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText(/persisted timeline/i)).toBeVisible();
  await expect(page.getByText(/exceptions stay narrow/i)).toBeVisible();
});

test("blocks an unsafe electrical exception", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /try safety exception/i }).click();
  await expect(page.getByText("ESCALATED", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("SAFETY_OCCUPANT_DANGER").first()).toBeVisible();
});
