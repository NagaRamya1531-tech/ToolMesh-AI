import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("shows real provider availability and keeps the Playground usable on mobile", async ({ page }, testInfo) => {
  await page.goto("/playground");
  await page.getByLabel("Identity").selectOption("support-agent");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_SUPPORT_PASSWORD ?? "local_support_password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByRole("button", { name: "OpenAI provider" }).click();
  if (process.env.TOOLMESH_TEST_OPENAI === "1") {
    await expect(page.getByText("Configured", { exact: true })).toBeVisible();
    await page.getByLabel("Sandbox task").fill("Check duplicate payment captures for the synthetic order O-102.");
    await page.getByLabel("Catalog search").fill("duplicate payment");
    const response = page.waitForResponse((item) => item.url().endsWith("/api/v1/playground/provider") && item.request().method() === "POST");
    await page.getByRole("button", { name: "Run read-only task" }).click();
    await testInfo.attach("support-provider-run", { body: JSON.stringify(await (await response).json()), contentType: "application/json" });
    await expect(page.getByRole("status", { name: "" }).filter({ hasText: "SUCCEEDED" })).toBeVisible({ timeout: 25000 });
    await expect(page.getByText("payments.check_duplicate_payment", { exact: true })).toBeVisible();
    await expect(page.getByText("Estimated cost", { exact: true })).toBeVisible();
  } else {
    await expect(page.getByText("Disabled", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: "Run read-only task" })).toHaveCount(0);
  }
  await page.screenshot({ path: testInfo.outputPath("openai-playground.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(audit.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("openai-playground-mobile.png"), fullPage: true });
});
