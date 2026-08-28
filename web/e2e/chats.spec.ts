import { expect, test } from "@playwright/test";
import { ask, signIn, signUp, uniqueEmail } from "./helpers";

test.describe("conversations", () => {
  test("a follow-up question resolves against the previous turn", async ({ page }) => {
    // The behaviour this whole feature exists for: before it, every question
    // started a fresh conversation and a follow-up had nothing to refer to.
    await signUp(page, uniqueEmail("followup"));
    await ask(page, "Which pickup zones are busiest?");
    await ask(page, "What is the average fare by borough?");

    const answers = page.getByTestId("answer");
    await expect(answers).toHaveCount(2);
    await expect(answers.first()).toContainText(/manhattan/i);
    // A second turn answering the first question again is the failure mode.
    await expect(answers.last()).toContainText(/borough/i);
  });

  test("a past conversation is listed and reopens with its transcript", async ({ page }) => {
    const email = uniqueEmail("reopen");
    await signUp(page, email);
    await ask(page, "Which pickup zones are busiest?");
    await ask(page, "What is the average fare by borough?");

    // Title comes from the opening question, derived server-side.
    await expect(page.getByTestId("chat-item")).toHaveCount(1);
    await expect(page.getByTestId("chat-item")).toContainText(/pickup zones/i);

    await page.getByTestId("new-chat").click();
    await expect(page.getByTestId("empty-state")).toBeVisible();

    await page.getByTestId("chat-item").first().click();
    await expect(page.getByTestId("answer")).toHaveCount(2);
    // The trace is stored with the transcript, not re-derived from a live
    // stream, so it has to come back too. It lives in a collapsed <details>.
    await page.getByTestId("trace-toggle").first().click();
    await expect(page.getByTestId("trace-row").first()).toBeVisible();
    await expect(page.getByTestId("trace-row").first()).toHaveAttribute(
      "data-tool",
      /\w+/,
    );
  });

  test("a conversation survives signing out and back in", async ({ page }) => {
    const email = uniqueEmail("durable");
    await signUp(page, email);
    await ask(page, "Which pickup zones are busiest?");
    await expect(page.getByTestId("chat-item")).toHaveCount(1);

    await page.getByTestId("sign-out").click();
    await signIn(page, email);
    await expect(page.getByTestId("chat-item")).toHaveCount(1);
  });

  test("archiving hides a chat without deleting it", async ({ page }) => {
    await signUp(page, uniqueEmail("archive"));
    await ask(page, "Which pickup zones are busiest?");

    await page.getByTestId("chat-menu").first().click();
    await page.getByTestId("archive-chat").click();
    await expect(page.getByTestId("chat-item")).toHaveCount(0);

    await page.getByRole("button", { name: "Archived" }).click();
    await expect(page.getByTestId("chat-item")).toHaveCount(1);
  });

  test("a chat can be renamed", async ({ page }) => {
    await signUp(page, uniqueEmail("rename-chat"));
    await ask(page, "Which pickup zones are busiest?");

    await page.getByTestId("chat-menu").first().click();
    await page.getByRole("button", { name: "Rename" }).click();
    await page.getByTestId("rename-input").fill("Zone demand");
    await page.getByTestId("rename-input").press("Enter");
    await expect(page.getByTestId("chat-item")).toContainText("Zone demand");
  });

  test("deleting a chat warns that the audit log is kept", async ({ page }) => {
    // The one place this app must not be vague about what it is doing.
    await signUp(page, uniqueEmail("delete"));
    await ask(page, "Which pickup zones are busiest?");

    let prompt = "";
    page.on("dialog", async (dialog) => {
      prompt = dialog.message();
      await dialog.accept();
    });

    await page.getByTestId("chat-menu").first().click();
    await page.getByTestId("delete-chat").click();
    await expect(page.getByTestId("chat-item")).toHaveCount(0);

    expect(prompt).toMatch(/audit log is not/i);
    expect(prompt).toMatch(/stays recorded|remains|organisation/i);
  });

  test("one account cannot see another's chats", async ({ page }) => {
    await signUp(page, uniqueEmail("owner"));
    await ask(page, "Which pickup zones are busiest?");
    await expect(page.getByTestId("chat-item")).toHaveCount(1);

    await page.getByTestId("sign-out").click();
    await signUp(page, uniqueEmail("stranger"));
    await expect(page.getByTestId("chat-item")).toHaveCount(0);
  });
});
