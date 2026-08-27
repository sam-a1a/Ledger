import { expect, test } from "@playwright/test";
import { signUp, uniqueEmail } from "./helpers";

test("the trace lists every call with row counts and durations", async ({ page }) => {
  await signUp(page, uniqueEmail("spec"));
  await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");

  await page.getByTestId("trace-toggle").click();

  const rows = page.getByTestId("trace-row");
  await expect(rows).toHaveCount(2); // top_n, then plot
  await expect(rows.first()).toHaveAttribute("data-tool", "top_n");
  await expect(rows.last()).toHaveAttribute("data-tool", "plot");

  // Every call reports how much data it touched and how long it took. A trace
  // that lists calls without those is a debug print, not a record.
  for (const cell of await page.getByTestId("trace-rows").all()) {
    await expect(cell).not.toHaveText("—");
  }
  for (const cell of await page.getByTestId("trace-ms").all()) {
    await expect(cell).not.toHaveText("—");
  }
});
