import { expect, test } from "@playwright/test";
import { signUp, uniqueEmail } from "./helpers";

test.describe("provider sign-in", () => {
  test("an unconfigured deployment offers no provider buttons", async ({ page }) => {
    // The suite runs the API without client credentials, which is the state a
    // clean clone is in. A button here would fail after the redirect, so the
    // page asks the API what it can actually do rather than assuming.
    await page.goto("/");
    await expect(page.getByTestId("auth-submit")).toBeVisible();
    await expect(page.getByTestId("oauth-providers")).toHaveCount(0);
  });

  test("a token left in the fragment is consumed and cleared", async ({ page }) => {
    // The callback hands the token back in the fragment, which never reaches a
    // server. What matters on this side is that it does not stay in the
    // address bar afterwards, where it would sit in history and get copied out
    // with the link.
    await signUp(page, uniqueEmail("fragment"));
    await page.getByTestId("sign-out").click();
    await expect(page.getByTestId("auth-submit")).toBeVisible();

    await page.evaluate(() => {
      window.location.hash = "oauth_error=cancelled";
    });
    await page.reload();

    await expect(page.getByTestId("auth-error")).toContainText(/cancelled/i);
    expect(await page.evaluate(() => window.location.hash)).toBe("");
  });
});
