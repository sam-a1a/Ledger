import { expect, test } from "@playwright/test";
import { analystEmail, ask, signUp, uniqueEmail } from "./helpers";

test("a viewer is refused tip data and an analyst is not", async ({ page }) => {
  // Analyst by allowlisted domain rather than by signup order, so this spec
  // asserts the boundary rather than asserting which test ran first.
  await signUp(page, analystEmail("analyst"), "Analyst");
  await expect(page.getByTestId("role-badge")).toHaveText("analyst");

  await ask(page, "What is the average tip by payment type?");
  await page.getByTestId("trace-toggle").last().click();
  await expect(page.getByTestId("trace-row").first()).toHaveAttribute(
    "data-tool",
    "aggregate",
  );
  await expect(page.getByTestId("answer")).toContainText(/card|cash/i);

  await page.getByTestId("sign-out").click();
  await signUp(page, uniqueEmail("viewer"), "Viewer");
  await expect(page.getByTestId("role-badge")).toHaveText("viewer");

  await ask(page, "What is the average tip by payment type?");
  const refusal = page.getByTestId("answer");
  await expect(refusal).toContainText(/do not have|not available/i);
  await expect(page.getByTestId("chart-card")).toHaveCount(0);

  // The refusal must not read as "you lack permission", which would confirm
  // the column is real and merely withheld.
  await expect(refusal).not.toContainText(/permission|restricted|forbidden|not allowed/i);
});
