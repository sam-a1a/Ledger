import { expect, test } from "@playwright/test";
import { analystEmail, signUp, uniqueEmail } from "./helpers";

test.describe("catalogue drawer", () => {
  test("lists columns with their profile and description provenance", async ({ page }) => {
    await signUp(page, analystEmail("catalog"));
    await page.getByTestId("catalog-toggle").click();

    const drawer = page.getByTestId("catalog-drawer");
    await expect(drawer).toBeVisible();
    await expect(page.getByTestId("catalog-meta")).toContainText(/analyst/);

    const column = page.getByTestId("catalog-column").filter({ hasText: "trip_distance" });
    await expect(column).toContainText(/%\s*null/);
    await expect(column).toContainText(/distinct/);
    // Whether a description was written, generated, or derived from the
    // profile is governance metadata, so it is on the page rather than in a
    // JSON field nobody reads.
    await expect(column.getByTestId("catalog-provenance")).toBeVisible();

    await page.getByTestId("catalog-filter").fill("borough");
    await expect(page.getByTestId("catalog-column")).toHaveCount(2);

    await page.getByTestId("catalog-close").click();
    await expect(drawer).toHaveCount(0);
  });

  test("a viewer's catalogue is visibly shorter than an analyst's", async ({ page }) => {
    // The access boundary as something you can see, rather than something you
    // discover by being refused. This is the reason the drawer exists.
    await signUp(page, analystEmail("catalog-analyst"));
    await page.getByTestId("catalog-toggle").click();
    // The list is fetched, so wait for it before counting -- an empty drawer
    // is trivially "shorter" and would make this assert nothing.
    await expect(page.getByTestId("catalog-column").first()).toBeVisible();
    const analystColumns = await page.getByTestId("catalog-column").count();
    await page.getByTestId("catalog-close").click();

    await page.getByTestId("sign-out").click();
    await signUp(page, uniqueEmail("catalog-viewer"));
    await expect(page.getByTestId("role-badge")).toHaveText("viewer");

    await page.getByTestId("catalog-toggle").click();
    await expect(page.getByTestId("catalog-column").first()).toBeVisible();
    const viewerColumns = await page.getByTestId("catalog-column").count();

    expect(analystColumns).toBeGreaterThan(0);
    expect(viewerColumns).toBeLessThan(analystColumns);
    await expect(page.getByTestId("catalog-drawer")).not.toContainText("tip_amount");
  });
});
