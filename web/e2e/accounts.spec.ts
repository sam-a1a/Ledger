import { expect, test } from "@playwright/test";
import { PASSWORD, signIn, signUp, uniqueEmail } from "./helpers";

test.describe("accounts", () => {
  test("sign up, sign out, and sign back in", async ({ page }) => {
    const email = uniqueEmail("roundtrip");
    await signUp(page, email, "Round Trip");
    await expect(page.getByTestId("open-settings")).toContainText("Round Trip");

    await page.getByTestId("sign-out").click();
    await expect(page.getByTestId("auth-form")).toBeVisible();

    await signIn(page, email);
    await expect(page.getByTestId("open-settings")).toContainText("Round Trip");
  });

  test("a wrong password says nothing about whether the account exists", async ({ page }) => {
    const email = uniqueEmail("enumeration");
    await signUp(page, email);
    await page.getByTestId("sign-out").click();

    // Same message for a real account with the wrong password...
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-password").fill("the-wrong-password");
    await page.getByTestId("auth-submit").click();
    const wrongPassword = await page.getByTestId("auth-error").textContent();

    // ...and for an address that was never registered.
    await page.getByTestId("auth-email").fill(uniqueEmail("ghost"));
    await page.getByTestId("auth-password").fill("the-wrong-password");
    await page.getByTestId("auth-submit").click();
    const noAccount = await page.getByTestId("auth-error").textContent();

    expect(wrongPassword).toBe(noAccount);
    expect(wrongPassword).not.toMatch(/no such|not found|unknown|exist/i);
  });

  test("the session survives a reload", async ({ page }) => {
    await signUp(page, uniqueEmail("persist"));
    await page.reload();
    await expect(page.getByTestId("sidebar")).toBeVisible();
  });

  test("password reset issues a working token", async ({ page }) => {
    const email = uniqueEmail("reset");
    await signUp(page, email);
    await page.getByTestId("sign-out").click();

    await page.getByRole("button", { name: /forgot password/i }).click();
    await page.getByTestId("auth-email").fill(email);
    await page.getByTestId("auth-submit").click();

    // In development the server returns the token rather than mailing it, and
    // says so, so the flow is exercisable without a mail provider.
    await expect(page.getByTestId("auth-notice")).toContainText(/development/i);
    await expect(page.getByTestId("auth-token")).not.toBeEmpty();

    await page.getByTestId("auth-password").fill("a-brand-new-password");
    await page.getByTestId("auth-submit").click();
    await expect(page.getByTestId("sidebar")).toBeVisible();
  });

  test("settings shows the role as read-only", async ({ page }) => {
    await signUp(page, uniqueEmail("settings"));
    await page.getByTestId("open-settings").click();
    await expect(page.getByTestId("settings")).toBeVisible();

    const role = page.locator("label", { hasText: "Role" }).locator("input");
    await expect(role).toBeDisabled();
    await expect(page.getByTestId("display-name")).toBeEditable();
  });

  test("display name can be changed", async ({ page }) => {
    await signUp(page, uniqueEmail("rename"), "Before");
    await page.getByTestId("open-settings").click();
    await page.getByTestId("display-name").fill("After");
    await page.getByRole("button", { name: "Save profile" }).click();
    await expect(page.getByTestId("settings-message")).toContainText(/saved/i);
    await expect(page.getByTestId("open-settings")).toContainText("After");
  });

  test("password can be changed with the current one", async ({ page }) => {
    await signUp(page, uniqueEmail("password"));
    await page.getByTestId("open-settings").click();
    await page.getByTestId("current-password").fill(PASSWORD);
    await page.getByTestId("new-password").fill("a-replacement-password");
    await page.getByRole("button", { name: "Change password" }).click();
    await expect(page.getByTestId("settings-message")).toContainText(/changed/i);
  });
});
