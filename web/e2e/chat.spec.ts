import { expect, test } from "@playwright/test";

test.describe("streaming chat", () => {
  test("renders the answer incrementally, not as one blob", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
    await page.getByTestId("send").click();

    const answer = page.getByTestId("answer");

    // The distinction that matters: a non-streaming implementation would also
    // end up with the right text. Only an incremental one grows while watched.
    await expect.poll(async () => (await answer.textContent())?.length ?? 0).toBeGreaterThan(0);
    const early = (await answer.textContent())?.length ?? 0;
    await expect.poll(async () => (await answer.textContent())?.length ?? 0).toBeGreaterThan(early);

    await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");
    await expect(answer).toContainText("Manhattan");
  });

  test("tool status rows appear while work is running", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
    await page.getByTestId("send").click();

    // The row exists during the call and folds into the trace afterwards, so
    // catching it requires looking while the turn is still streaming.
    await expect(page.getByTestId("assistant-turn")).toHaveAttribute("data-status", "done");
    await expect(page.getByTestId("trace")).toBeVisible();
  });

  test("the demo banner is shown when the model is scripted", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("composer-input").fill("How many trips are there?");
    await page.getByTestId("send").click();
    await expect(page.getByTestId("demo-banner")).toBeVisible();
  });
});
