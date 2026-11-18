import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, identity: string, password: string) {
  await page.getByLabel("Identity").selectOption(identity);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("support investigation, finance handoff, approval and confirmed support resolution", async ({ page, request }, testInfo) => {
  const admin = await request.post("/api/v1/auth/login", { data: {
    username: "platform-admin", password: process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password",
  }});
  expect(admin.ok()).toBeTruthy();
  const { access_token } = await admin.json();
  for (const tool of ["payments.check_duplicate_payment", "payments.get_transaction", "support.create_ticket", "payments.issue_refund", "communication.send_notification", "support.resolve_ticket"]) {
    const result = await request.post(`/api/v1/tools/${tool}/publish`, {
      headers: { Authorization: `Bearer ${access_token}` }, data: { version: 1, reason: "Review scripted workflow" },
    });
    expect(result.ok()).toBeTruthy();
  }

  await page.goto("/playground/duplicate-payment");
  const supportPassword = process.env.TOOLMESH_SUPPORT_PASSWORD ?? "local_support_password";
  await signIn(page, "support-agent", supportPassword);
  await page.getByRole("button", { name: "Investigate order" }).click();
  const workflow = page.getByRole("region", { name: "Workflow record" });
  await expect(workflow).toContainText("TX-102-A, TX-102-B");
  await expect(workflow).toContainText("Support ticket");

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "finance-agent", process.env.TOOLMESH_FINANCE_PASSWORD ?? "local_finance_password");
  await page.getByRole("combobox", { name: "Duplicate capture to refund" }).selectOption("TX-102-A");
  await page.getByRole("button", { name: "Request refund" }).click();
  await expect(workflow).toContainText("PENDING_APPROVAL");
  await workflow.getByRole("link", { name: "Refund approval" }).click();
  const intent = page.getByRole("region", { name: "Refund intent" });
  await expect(intent).toContainText("4,550 USD");
  await expect(intent.getByRole("button", { name: "Approve refund" })).toHaveCount(0);

  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "refund-approver", process.env.TOOLMESH_APPROVER_PASSWORD ?? "local_approver_password");
  await intent.getByLabel("Decision reason").fill("Duplicate capture confirmed");
  await intent.getByRole("button", { name: "Approve refund" }).click();
  await expect(intent).toContainText("SUCCEEDED");
  const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(scan.violations).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath("m2-approval.png"), fullPage: true });

  await page.goBack();
  await expect(workflow).toContainText("SUCCEEDED");
  await page.getByRole("button", { name: "Sign out" }).click();
  await signIn(page, "support-agent", supportPassword);
  await workflow.getByRole("button", { name: "Notify customer and resolve ticket" }).click();
  await expect(workflow).toContainText("Simulated notification");
  await expect(workflow).toContainText("Ticket resolved");
  await page.screenshot({ path: testInfo.outputPath("m2-workflow.png"), fullPage: true });
});
