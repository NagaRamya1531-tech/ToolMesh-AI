import { expect, test } from "@playwright/test";

test("run, compare and export a real retrieval evaluation", async ({page},testInfo) => {
  await page.goto("/evaluations");
  await page.getByLabel("Identity").selectOption("platform-admin");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  await page.getByRole("button",{name:"Sign in",exact:true}).click();
  await page.getByLabel("Catalog definitions").fill("60");
  await page.getByLabel("Labeled cases").fill("10");
  await page.getByRole("button",{name:"Run evaluation"}).click();
  const result=page.getByRole("region",{name:"Evaluation run"});
  await expect(result.getByRole("status")).toHaveText("SUCCEEDED",{timeout:18000});
  await expect(result.getByRole("row",{name:/^hybrid/})).toBeVisible();
  await expect(result.getByRole("meter",{name:"keyword Recall at five"})).toBeVisible();
  const download=page.waitForEvent("download");
  await result.getByRole("button",{name:"Download CSV"}).click();
  expect((await download).suggestedFilename()).toMatch(/\.csv$/);
  await page.screenshot({path:testInfo.outputPath("m4-evaluations.png"),fullPage:true});
});

test("search with hybrid retrieval through the real Playground", async ({page}) => {
  await page.goto("/playground");
  await page.getByLabel("Identity").selectOption("payments-agent");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_PAYMENTS_PASSWORD ?? "local_payments_password");
  await page.getByRole("button",{name:"Sign in",exact:true}).click();
  await page.getByLabel("Search query").fill("duplicate capture");
  await page.getByRole("combobox",{name:"Discovery strategy"}).selectOption("hybrid");
  await page.getByRole("button",{name:"Search published tools"}).click();
  const tool=page.getByRole("combobox",{name:"Tool",exact:true});
  await expect(tool.getByRole("option",{name:"1. payments.check_duplicate_payment",exact:true})).toHaveCount(1);
  await expect(tool.getByRole("option",{name:/support\./})).toHaveCount(0);
});
