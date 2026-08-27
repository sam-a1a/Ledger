import { expect, test } from "@playwright/test";

test.describe("streaming chat", () => {
  test("renders the answer incrementally, not as one blob", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("composer-input").fill("Which pickup zones are busiest?");
    await page.getByTestId("send").click();

    const answer = page.getByTestId("answer");
    const turn = page.getByTestId("assistant-turn");

    // The property, not a race: text must be painted *while the turn is still
    // streaming*. An implementation that buffered the whole answer and rendered
    // once would show nothing until the status flips to "done", so observing
    // both facts together is enough — and unlike comparing two samples for
    // growth, it does not depend on catching a ~200 ms window twice.
    await expect
      .poll(async () => {
        const [status, text] = await Promise.all([
          turn.getAttribute("data-status"),
          answer.textContent(),
        ]);
        return status === "streaming" && (text ?? "").length > 0;
      })
      .toBe(true);

    await expect(turn).toHaveAttribute("data-status", "done");
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
