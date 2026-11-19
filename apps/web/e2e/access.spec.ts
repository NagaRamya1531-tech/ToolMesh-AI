import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

async function signIn(page: Page, id: string, password: string) {
  await page.getByLabel("Identity").selectOption(id);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

async function publish(request: APIRequestContext, tools: string[]) {
  const response = await request.post("/api/v1/auth/login", {data:{
    username:"platform-admin",password:process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password",
  }});
  expect(response.ok()).toBeTruthy();
  const {access_token}=await response.json();
  for (const tool of tools) {
    const result=await request.post(`/api/v1/tools/${tool}/publish`,{headers:{Authorization:`Bearer ${access_token}`},
      data:{version:1,reason:"Verify M5 console workflow"}});
    expect(result.ok()).toBeTruthy();
  }
  await request.post("/api/v1/auth/logout",{headers:{Authorization:`Bearer ${access_token}`}});
}

test("edit, simulate and revise a scoped policy",async ({page,request},testInfo)=>{
  await publish(request,["hr.get_employee"]);
  await page.goto("/policies");
  await signIn(page,"platform-admin",process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  const editor=page.locator("form").filter({has:page.getByRole("heading",{name:"Create rule"})});
  const simulation=page.locator("form").filter({has:page.getByRole("heading",{name:"Simulate"})});
  await editor.getByRole("combobox",{name:"Principal"}).selectOption("hr-agent");
  await editor.getByRole("combobox",{name:"Tool"}).selectOption("hr.get_employee");
  await editor.getByRole("textbox",{name:"Reason"}).fill("Temporary review hold");
  await editor.getByRole("button",{name:"Create policy"}).click();
  const history=page.getByRole("region",{name:"Policy history"});
  await expect(history.getByText(/v1.*Temporary review hold/)).toBeVisible();
  await simulation.getByRole("combobox",{name:"Principal"}).selectOption("hr-agent");
  await simulation.getByRole("combobox",{name:"Tool"}).selectOption("hr.get_employee");
  await simulation.getByRole("textbox",{name:"Arguments"}).fill('{"customer_id":"C-102","employee_id":"E-102"}');
  await simulation.getByRole("button",{name:"Check access"}).click();
  await expect(simulation.getByRole("status")).toHaveText("Denied · EXPLICIT_DENY");
  await history.getByLabel("Revision reason").fill("Review complete");
  await history.getByRole("button",{name:"Disable rule"}).click();
  await expect(history.getByText(/v2.*Review complete/)).toBeVisible();
  const axe=await new AxeBuilder({page}).withTags(["wcag2a","wcag2aa"]).analyze();
  expect(axe.violations).toEqual([]);
  await page.screenshot({path:testInfo.outputPath("m5-policies.png")});

  await page.getByRole("link",{name:"Settings",exact:true}).click();
  await page.getByRole("spinbutton",{name:"Per-principal tool quota"}).fill("30");
  await page.getByRole("spinbutton",{name:"Global execution quota"}).fill("300");
  await page.getByRole("button",{name:"Save quotas"}).click();
  await expect(page.getByRole("status")).toContainText("Saved 30 per tool");

  await page.getByRole("link",{name:"Agents",exact:true}).click();
  await expect(page.getByRole("link",{name:"hr-agent",exact:true})).toBeVisible();
});

test("run the scripted Finance scenario and show its persisted report step",async ({page,request},testInfo)=>{
  await publish(request,["analytics.get_sales_metrics","analytics.get_refund_metrics","analytics.compare_periods","analytics.generate_report"]);
  await page.goto("/playground/scenarios");
  await signIn(page,"finance-agent",process.env.TOOLMESH_FINANCE_PASSWORD ?? "local_finance_password");
  await page.getByRole("button",{name:"Run scenario"}).click();
  const record=page.getByRole("region",{name:"Scenario record"});
  await expect(record.getByRole("status")).toHaveText("SUCCEEDED",{timeout:18000});
  await expect(record.getByText("analytics.generate_report · SUCCEEDED",{exact:true})).toBeVisible();
  await page.screenshot({path:testInfo.outputPath("m5-scenario.png")});
});
