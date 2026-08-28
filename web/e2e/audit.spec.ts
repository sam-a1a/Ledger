import { expect, test } from "@playwright/test";
import { analystEmail, ask, signUp } from "./helpers";

test("the trace reconciles against the durable audit log", async ({ page }) => {
  // The claim this asserts: the trace is a view over an event log, not just a
  // view over a stream. The live rows come from SSE, because reading the log
  // mid-answer would race the consumer. Afterwards the two must agree.
  //
  // Reaching `verified` means the whole governance path ran for real: the tool
  // call was published to Kafka before it executed, a separate consumer
  // process read it back off the topic, materialised it to parquet, and the
  // API served it from there.
  await signUp(page, analystEmail("audit"));
  await ask(page, "Which pickup zones are busiest?");

  await page.getByTestId("trace-toggle").last().click();

  const badge = page.getByTestId("audit-badge").last();
  await expect(badge).toBeVisible();
  // Polls on its own; the assertion just waits for the round trip to land.
  await expect(badge).toHaveAttribute("data-status", "verified", { timeout: 28_000 });
  await expect(badge).toContainText("audit");
});

test("a reopened conversation reconciles from the log with no stream at all", async ({
  page,
}) => {
  // The stronger form of the same claim. Here there is no SSE stream to derive
  // anything from: the trace comes from the stored transcript, and the badge
  // comes from the audit log. Two records written by different processes,
  // agreeing after the fact.
  await signUp(page, analystEmail("audit-reopen"));
  await ask(page, "Which pickup zones are busiest?");

  await page.getByTestId("new-chat").click();
  await expect(page.getByTestId("empty-state")).toBeVisible();

  await page.getByTestId("chat-item").first().click();
  await expect(page.getByTestId("answer")).toHaveCount(1);

  await page.getByTestId("trace-toggle").first().click();
  await expect(page.getByTestId("audit-badge").first()).toHaveAttribute(
    "data-status",
    "verified",
    { timeout: 28_000 },
  );
});
