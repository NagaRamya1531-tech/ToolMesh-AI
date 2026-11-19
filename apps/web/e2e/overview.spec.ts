import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

for (const width of [1440, 1024, 390]) {
  test(`live readiness at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
    await expect(page.getByRole("status")).toHaveText("Ready");
    await expect(page.getByText("available", { exact: true })).toHaveCount(2);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(width);

    if (width === 390) {
      await page.getByRole("button", { name: "Menu", exact: true }).focus();
      await page.keyboard.press("Enter");
      const drawer = page.getByRole("dialog", { name: "Console navigation" });
      await expect(drawer.getByRole("link", { name: "Overview" })).toBeVisible();
      await page.keyboard.press("Escape");
      await expect(drawer).toBeHidden();
      await expect(page.getByRole("button", { name: "Menu", exact: true })).toBeFocused();
    } else {
      await page.getByRole("button", { name: "Hide sidebar" }).click();
      await expect(page.getByRole("navigation", { name: "Console" })).toBeHidden();
      await page.getByRole("button", { name: "Show sidebar" }).click();
      await expect(page.getByRole("navigation", { name: "Console" })).toBeVisible();
    }

    const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    expect(scan.violations).toEqual([]);
    const refreshed = page.waitForResponse((response) => new URL(response.url()).pathname === "/readyz");
    await page.getByRole("button", { name: "Refresh" }).click();
    expect((await refreshed).status()).toBe(200);
    await expect(page.getByRole("button", { name: "Refresh" })).toBeEnabled();
    await page.screenshot({ path: testInfo.outputPath(`overview-${width}.png`) });
  });
}

test("retries after a lost connection", async ({ page }) => {
  await page.route("**/readyz", (route) => route.abort());
  await page.goto("/");
  await expect(page.getByRole("status")).toHaveText("Gateway unreachable");
  await page.unroute("**/readyz");
  await page.getByRole("button", { name: "Retry" }).click();
  await expect(page.getByRole("status")).toHaveText("Ready");
});

test("signed-in activity range and execution links reflect the gateway", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1060 });
  await page.goto("/services");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Services" })).toBeVisible();
  await page.getByRole("navigation", { name: "Console" }).getByRole("link", { name: "Overview" }).click();
  const activity = page.getByRole("region", { name: "Execution activity" });
  await expect(activity.getByRole("link", { name: /Executions in range/ })).toHaveAttribute("href", "/executions?hours=24");
  const refreshed = page.waitForResponse((response) => new URL(response.url()).searchParams.get("hours") === "168");
  await activity.getByLabel("Time range").selectOption("168");
  const response = await refreshed;
  expect(response.ok()).toBeTruthy();
  const data = await response.json();
  const count = data.outcomes.reduce((total: number, entry: { count: number }) => total + entry.count, 0);
  const drilldown = activity.getByRole("link", { name: /Executions in range/ });
  await expect(drilldown).toContainText(count.toLocaleString());
  await expect(drilldown).toHaveAttribute("href", "/executions?hours=168");
  const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(scan.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("overview-workspace.png"), fullPage: true });
  await drilldown.click();
  await expect(page).toHaveURL(/executions\?hours=168$/);
});
