import { expect, test } from "@playwright/test";

test("a viewer is refused tip data and an analyst is not", async ({ page }) => {
  await page.goto("/");

  await page.getByTestId("role-viewer").click();
  await page.getByTestId("composer-input").fill("What is the average tip by payment type?");
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");

  const refusal = page.getByTestId("answer");
  await expect(refusal).toContainText(/do not have|not available/i);
  // Refused, and never charted.
  await expect(page.getByTestId("chart-card")).toHaveCount(0);

  // The refusal must not read as "you lack permission", which would confirm the
  // column exists. It reads exactly like a column that is not there.
  await expect(refusal).not.toContainText(/permission|restricted|forbidden|not allowed/i);

  await page.getByTestId("role-analyst").click();
  await page.getByTestId("composer-input").fill("What is the average tip by payment type?");
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");

  // The same question, answered, which is what makes the refusal meaningful.
  await page.getByTestId("trace-toggle").click();
  await expect(page.getByTestId("trace-row").first()).toHaveAttribute("data-tool", "aggregate");
  await expect(page.getByTestId("answer")).toContainText(/card|cash/i);
});
