import { expect, test } from "@playwright/test";

/**
 * Produces the README screenshots from the real running app, so they cannot
 * go stale. Invoked explicitly via `npm run screenshots`.
 */
test("@screenshot capture the interface", async ({ page }) => {
  await page.setViewportSize({ width: 1340, height: 900 });
  await page.goto("/");

  // Sign-up, which also makes this the first account and therefore an analyst.
  await page.getByTestId("go-signup").click();
  await page.getByTestId("auth-email").fill("sam@example.com");
  await page.getByTestId("auth-name").fill("Sam");
  await page.getByTestId("auth-password").fill("correct-horse-battery");
  await page.screenshot({ path: "../docs/img/signup.png" });
  await page.getByTestId("auth-submit").click();
  await expect(page.getByTestId("sidebar")).toBeVisible();

  const ask = async (question: string) => {
    await page.getByTestId("composer-input").fill(question);
    await page.getByTestId("send").click();
    await expect(page.getByTestId("assistant-turn").last()).toHaveAttribute(
      "data-status",
      "done",
    );
  };

  await ask("Which pickup zones are busiest?");
  await ask("What is the average fare by borough?");
  await page.getByTestId("new-chat").click();
  await ask("How did fares change after the congestion charge?");

  await expect(page.getByTestId("chat-item")).toHaveCount(2);
  await page.getByTestId("trace-toggle").last().click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: "../docs/img/chat.png" });

  // The multi-turn conversation, reopened from the sidebar.
  await page.getByTestId("chat-item").last().click();
  await expect(page.getByTestId("assistant-turn").first()).toBeVisible();
  await page.waitForTimeout(300);
  await page.screenshot({ path: "../docs/img/conversations.png" });

  await page.getByTestId("open-settings").click();
  await expect(page.getByTestId("settings")).toBeVisible();
  await page.screenshot({ path: "../docs/img/settings.png" });
});
