import { expect, type Page } from "@playwright/test";

export const PASSWORD = "correct-horse-battery";

/** A fresh address per call, so tests never collide on a unique constraint. */
export const uniqueEmail = (label: string) =>
  `${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@example.com`;

/**
 * An address the API promotes to analyst, via `LEDGER_ANALYST_EMAILS`.
 *
 * The alternative -- relying on "the first account created is an analyst" --
 * only holds for whichever spec runs first, so it passed alone and failed in
 * the suite.
 */
export const analystEmail = (label: string) =>
  `${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}@analyst.example.com`;

export async function signUp(page: Page, email: string, name = "Tester"): Promise<void> {
  await page.goto("/");
  await page.getByTestId("go-signup").click();
  await page.getByTestId("auth-email").fill(email);
  await page.getByTestId("auth-name").fill(name);
  await page.getByTestId("auth-password").fill(PASSWORD);
  await page.getByTestId("auth-submit").click();
  await expect(page.getByTestId("sidebar")).toBeVisible();
}

export async function signIn(page: Page, email: string): Promise<void> {
  await page.goto("/");
  await page.getByTestId("auth-email").fill(email);
  await page.getByTestId("auth-password").fill(PASSWORD);
  await page.getByTestId("auth-submit").click();
  await expect(page.getByTestId("sidebar")).toBeVisible();
}

export async function ask(page: Page, question: string): Promise<void> {
  await page.getByTestId("composer-input").fill(question);
  await page.getByTestId("send").click();
  await expect(page.getByTestId("assistant-turn").last()).toHaveAttribute(
    "data-status",
    "done",
  );
}
