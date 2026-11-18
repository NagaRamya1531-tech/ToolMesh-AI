import { expect, test } from "@playwright/test";

test("register an allowlisted service, crawl gRPC descriptors and invoke the published tool", async ({page},testInfo)=>{
  await page.goto("/services");
  await page.getByLabel("Identity").selectOption("platform-admin");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_ADMIN_PASSWORD ?? "local_admin_password");
  await page.getByRole("button",{name:"Sign in",exact:true}).click();
  await expect(page.getByRole("heading",{name:"Services",exact:true})).toBeVisible();
  if (!(await page.getByRole("link",{name:"IT Operations",exact:true}).isVisible())) {
    await page.getByText("Register a service",{exact:true}).click();
    await page.getByRole("combobox",{name:"Allowlisted target"}).selectOption("it");
    await page.getByLabel("Display name").fill("IT Operations");
    await page.getByRole("button",{name:"Register service"}).click();
  }
  await page.getByRole("link",{name:"IT Operations",exact:true}).click();
  await page.getByRole("button",{name:"Test connection"}).click();
  await expect(page.getByRole("status")).toContainText("healthy");
  await page.getByRole("link",{name:"Open crawler"}).click();
  await page.getByRole("button",{name:"Run crawl"}).click();
  const job=page.getByRole("region",{name:"Crawl job"});
  await expect(job.getByRole("status")).toHaveText("SUCCEEDED",{timeout:12000});
  await page.screenshot({path:testInfo.outputPath("m3-crawler.png")});
  await job.getByRole("link",{name:"it.check_service_health v1"}).click();
  const contract=page.getByRole("region",{name:"Tool contract"});
  await expect(contract.getByRole("heading",{name:"Input schema"})).toBeVisible();
  const publish=contract.getByRole("button",{name:"Publish version"});
  if (await publish.isVisible()) {
    await contract.getByLabel("Publication reason").fill("Reviewed descriptor contract");
    await publish.click();
    await expect(page.getByRole("status")).toContainText("Published it.check_service_health v1.");
  }
  await page.getByRole("button",{name:"Sign out"}).click();
  await page.getByLabel("Identity").selectOption("it-agent");
  await page.getByLabel("Password").fill(process.env.TOOLMESH_IT_PASSWORD ?? "local_it_agent_password");
  await page.getByRole("button",{name:"Sign in",exact:true}).click();
  await expect(page.getByRole("heading",{name:"Registry",exact:true})).toBeVisible();
  await page.getByRole("link",{name:"Playground",exact:true}).click();
  await page.getByRole("combobox",{name:"Tool",exact:true}).selectOption("it.check_service_health");
  await page.getByLabel("Arguments").fill('{"customer_id":"C-102","service_id":"sandbox-api"}');
  await page.getByRole("button",{name:"Invoke tool"}).click();
  await expect(page.getByRole("status")).toHaveText("SUCCEEDED");
  await expect(page.getByText("degraded",{exact:false})).toBeVisible();
});
