import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

test("every sidebar page uses its workspace section and keeps its controls visible", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/services");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const navigation = page.getByRole("navigation", { name: "Console" });
  for (const [pageName, section, control, role] of [
    ["Services", "platform", "Protocol", "combobox"],
    ["Registry", "platform", "Service", "combobox"],
    ["AutoCrawler", "platform", "Service", "combobox"],
    ["Agents", "access", "Agent ID", "textbox"],
    ["Policies", "access", "Filter principal", "combobox"],
    ["Approvals", "access", "Status", "combobox"],
    ["Playground", "operations", "Discovery strategy", "combobox"],
    ["Executions", "operations", "Status", "combobox"],
    ["Audit", "operations", "Action", "combobox"],
    ["Evaluations", "quality", "Catalog definitions", "spinbutton"],
    ["Settings", "administration", "Per-principal tool quota", "spinbutton"],
  ] as const) {
    const link = navigation.getByRole("link", { name: pageName, exact: true });
    await link.click();
    await expect(link).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("heading", { name: pageName, exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toHaveAttribute("data-section", section);
    await expect(page.getByRole(role, { name: control, exact: true })).toBeVisible();
    if (["Agents", "Evaluations", "Settings"].includes(pageName)) {
      const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      expect(scan.violations).toEqual([]);
      await page.screenshot({ path: testInfo.outputPath(`${pageName.toLowerCase()}.png`), fullPage: true });
    }
  }
  await navigation.getByRole("link", { name: "Registry", exact: true }).click();
  await page.getByRole("link", { name: "payments.check_duplicate_payment", exact: true }).click();
  const contract = page.getByRole("region", { name: "Tool contract" });
  await expect(contract.getByRole("heading", { name: "Input schema" })).toBeVisible();
  await expect(page.locator('tr[data-selected="true"]')).toContainText("payments.check_duplicate_payment");
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath("registry.png") });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.getByRole("button", { name: "Menu", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Console navigation" });
  await drawer.getByRole("link", { name: "Agents", exact: true }).click();
  await expect(drawer).toBeHidden();
  await expect(page.getByRole("heading", { name: "Agents", exact: true })).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await page.screenshot({ path: testInfo.outputPath("agents-mobile.png"), fullPage: true });
});
