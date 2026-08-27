import { expect, test } from "@playwright/test";

/**
 * Not a test of behaviour -- it produces the README screenshots, so they are
 * always of the real running app rather than a stale mock-up.
 * Excluded from the normal run by its @screenshot tag.
 */
test("@screenshot capture the interface", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto("/");
  await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");
  await expect(page.getByTestId("chart-card")).toBeVisible();
  await page.waitForTimeout(600); // let the chart finish its entry animation
  await page.screenshot({ path: "../docs/img/chat.png", fullPage: false });

  await page.getByTestId("trace-toggle").click();
  await expect(page.getByTestId("trace-row").first()).toBeVisible();
  await page.screenshot({ path: "../docs/img/trace.png", fullPage: false });

  await page.getByTestId("role-viewer").click();
  await page.getByTestId("composer-input").fill("What is the average tip by payment type?");
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");
  await page.getByTestId("trace-toggle").click();
  await page.screenshot({ path: "../docs/img/rbac.png", fullPage: false });
});
