import { expect, test } from "@playwright/test";
import { signUp, uniqueEmail } from "./helpers";

test("a chart renders with real dimensions", async ({ page }) => {
  await signUp(page, uniqueEmail("spec"));
  await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
  await page.getByTestId("send").click();

  const card = page.getByTestId("chart-card");
  await expect(card).toBeVisible();

  // A canvas that exists but has zero size is the usual failure, and it looks
  // identical to success in a screenshot-free assertion.
  const canvas = card.locator("canvas").first();
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  expect(box?.width ?? 0).toBeGreaterThan(100);
  expect(box?.height ?? 0).toBeGreaterThan(100);
});
