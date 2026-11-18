import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, identity: string, password: string) {
  await page.getByLabel("Identity").selectOption(identity);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("review a draft, publish, invoke with a separate identity and inspect the trace", async ({ page }, testInfo) => {
  const tool = "payments.check_duplicate_payment";
  await page.goto("/tools");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await signIn(page, "platform-admin", process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  const row = page.getByRole("row").filter({ has: page.getByRole("link", { name: tool, exact: true }) });
  await row.getByRole("link").click();
  await expect(page.getByRole("heading", { name: "Input schema" })).toBeVisible();
  const draft = row.getByText("Draft", { exact: true });
  if (process.env.TOOLMESH_EXPECT_DRAFT) await expect(draft).toBeVisible();
  if (await draft.isVisible()) {
    await page.getByLabel("Publication reason").fill("Reviewed duplicate capture contract");
    await page.getByRole("button", { name: "Publish version" }).click();
    await expect(page.getByRole("status")).toContainText(`Published ${tool}`);
  }
  await expect(row.getByText("Published", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("registry.png"), fullPage: true });

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await signIn(page, "support-agent", process.env.TOOLMESH_SUPPORT_PASSWORD ?? "local_support_password");
  await page.getByRole("link", { name: "Services", exact: true }).click();
  await page.getByLabel("Protocol").selectOption("REST");
  await expect(page.getByRole("link", { name: "Payments", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Customer Support", exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(/protocol=REST/);

  await page.getByRole("link", { name: "Playground", exact: true }).click();
  await page.getByRole("combobox", { name: "Tool", exact: true }).selectOption(tool);
  await page.getByLabel("Arguments").fill('{"customer_id":"C-102","order_id":"O-102"}');
  await page.getByRole("button", { name: "Invoke tool" }).click();
  await expect(page.getByRole("status")).toHaveText("SUCCEEDED");
  await expect(page.getByText("TX-102-B", { exact: false })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("playground.png"), fullPage: true });
  await page.getByRole("link", { name: "Open execution trace" }).click();
  await expect(page.getByRole("heading", { name: "Execution trace" })).toBeVisible();
  await expect(page.locator("dd").filter({ hasText: /^support-agent$/ })).toBeVisible();
  await expect(page.locator("dd").filter({ hasText: /^SUCCEEDED$/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("executions.png"), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  const audit = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(audit.violations).toEqual([]);
});
