import { test, expect } from "@playwright/test";
import type { Page, TestInfo } from "@playwright/test";

test.skip(process.env.PLATFORM_ORGANIZATION_ACCEPTANCE !== "1", "Requires a fresh organization_acceptance_server.py database");
test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1280, height: 1024 } });

async function login(page: Page, route: string, email = "owner@example.test") {
  await page.goto(`/#${route}`);
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill("Agent-ui-acceptance-2026");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
}
async function shot(page: Page, info: TestInfo, name: string) {
  await page.evaluate(async () => { await document.fonts.ready; await Promise.all(Array.from(document.images).map(image => image.decode())); });
  await page.screenshot({ path: info.outputPath(`${name}.png`), animations: "disabled", caret: "hide" });
}
async function request(page: Page, path: string, method = "GET", body?: unknown) {
  return page.evaluate(async ({ path, method, body }) => {
    const identity = await fetch("/api/v2/me").then(response => response.json());
    const tenant = sessionStorage.getItem("agent-platform.tenant");
    const response = await fetch(`/api/v2/tenants/${tenant}${path}`, { method, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": identity.csrf_token }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }, { path, method, body });
}

test("成员列表、标签、搜索、真实邀请和 Logo 展开", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await login(page, "users");
  await expect(page.locator(".organization-members-table tbody tr")).toHaveCount(3);
  await page.getByLabel("在已加载成员中搜索...").fill("dev@example.test");
  await expect(page.locator(".organization-members-table tbody tr")).toHaveCount(1);
  await page.getByLabel("在已加载成员中搜索...").fill("");
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  const logo = page.getByRole("button", { name: "展开侧栏", exact: true });
  await expect(logo).toHaveText("Z");
  await expect(logo).not.toHaveAttribute("title");
  await logo.hover();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await shot(page, info, "members-collapsed");
  await logo.click();
  await page.getByRole("button", { name: "+ 邀请成员", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "邀请组织成员" });
  await dialog.getByLabel("成员邮箱").fill("qa@example.test");
  await dialog.getByLabel("加入后的角色").selectOption("finance_viewer");
  const created = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/invitations"));
  await dialog.getByRole("button", { name: "创建邀请", exact: true }).click();
  expect((await created).status()).toBe(201);
  await expect(page.getByLabel("邀请链接")).toHaveValue(/token=/);
  await shot(page, info, "invitation-created");
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("tab", { name: "邀请记录", exact: true }).click();
  await expect(page.locator('[role="tabpanel"]')).toContainText("qa@example.test");
  expect(errors).toEqual([]);
});

test("成员编辑保留权限版本，所有权选择与取消", async ({ page }, info) => {
  await login(page, "users");
  const member = page.locator(".organization-members-table tbody tr").filter({ hasText: "dev@example.test" });
  await member.getByRole("button", { name: "管理", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "编辑组织成员" });
  await dialog.getByLabel("组织角色", { exact: true }).selectOption("finance_viewer");
  const patched = page.waitForResponse(response => response.request().method() === "PATCH" && response.url().includes("/memberships/"));
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await patched).status()).toBe(200);
  await expect(member).toContainText("finance_viewer");
  await member.getByRole("button", { name: "管理", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "编辑组织成员" });
  await dialog.getByLabel("组织角色", { exact: true }).selectOption("member");
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  await expect(member).toContainText("member");
  await page.getByRole("button", { name: "转移所有权", exact: true }).click();
  await page.getByLabel("新的组织所有者", { exact: true }).selectOption("acceptance_member-membership");
  await page.getByRole("button", { name: "继续", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "转移组织所有权" })).toContainText("dev@example.test");
  await shot(page, info, "ownership-confirmation-inferred");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  const members = await request(page, "/memberships");
  expect(members.body.items.find((item: { email: string; role: string }) => item.email === "owner@example.test").role).toBe("owner");
});

test("内部配额真实保存、零值及只读套餐", async ({ page }, info) => {
  await login(page, "quotas");
  const user = page.locator(".organization-quotas-table tbody tr").filter({ hasText: "dev@example.test" });
  await user.getByRole("button", { name: "编辑", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "配置配额策略" });
  await expect(dialog.getByLabel("作用范围")).toBeDisabled();
  await dialog.getByLabel("每分钟请求数（RPM）").fill("0");
  const saved = page.waitForResponse(response => response.request().method() === "PUT" && response.url().endsWith("/quotas"));
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await saved).status()).toBe(200);
  const quotas = await request(page, "/quotas");
  const persisted = quotas.body.items.find((item: { scope: string }) => item.scope === "user");
  expect(persisted.rpm).toBe(0);
  expect(persisted.tpm).toBe(20000);
  expect(persisted.concurrent).toBe(2);
  const restored = await request(page, "/quotas", "PUT", { scope: "user", subject_id: persisted.subject_id, rpm: 10, tpm: persisted.tpm, concurrent: persisted.concurrent, max_budget: persisted.max_budget });
  expect(restored.status).toBe(200);
  await page.getByRole("tab", { name: "套餐上限", exact: false }).click();
  await expect(page.locator(".organization-entitlements")).toContainText("由平台运营配置");
  await expect(page.getByRole("button", { name: "+ 配置配额", exact: true })).toHaveCount(0);
  await shot(page, info, "entitlements-readonly-inferred");
});

test("审计本地筛选与返回数据详情抽屉", async ({ page }, info) => {
  await login(page, "audit");
  await page.getByLabel("筛选已加载记录...").fill("agent.publish");
  await expect(page.locator(".organization-audit-table tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "查看", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "审计记录详情" });
  await expect(dialog).toContainText("agent_calculator:2");
  await expect(dialog).toContainText("无附加信息");
  const box = await dialog.boundingBox();
  expect(Math.round(box!.x + box!.width)).toBe(1280);
  await shot(page, info, "audit-detail-inferred");
});

test("支持授权失败、真实批准与确认撤销", async ({ page }, info) => {
  await login(page, "support-access");
  await expect(page.locator(".organization-support-table tbody tr")).toHaveCount(2);
  await page.getByRole("button", { name: "批准支持访问", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "批准临时支持访问" });
  await dialog.getByLabel("支持人员邮箱").fill("dev@example.test");
  await dialog.getByLabel("排查原因").fill("组织模块功能验收");
  const failed = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/support-grants"));
  await dialog.getByRole("button", { name: "批准访问", exact: true }).click();
  expect((await failed).status()).toBe(403);
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByLabel("排查原因")).toHaveValue("组织模块功能验收");
  await shot(page, info, "support-request-error");
  await dialog.getByLabel("支持人员邮箱").fill("support@example.test");
  await expect(dialog.getByRole("checkbox")).not.toBeChecked();
  const approved = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/support-grants"));
  await dialog.getByRole("button", { name: "批准访问", exact: true }).click();
  const response = await approved;
  expect(response.status()).toBe(201);
  expect((await response.json()).allow_content).toBe(false);
  const grant = page.locator(".organization-support-table tbody tr").filter({ hasText: "组织模块功能验收" });
  await expect(grant).toContainText("有效");
  await grant.getByRole("button", { name: "撤销", exact: true }).click();
  await shot(page, info, "support-revoke-confirmation-inferred");
  const revoked = page.waitForResponse(response => response.request().method() === "DELETE" && response.url().includes("/support-grants/"));
  await page.getByRole("button", { name: "确认撤销", exact: true }).click();
  expect((await revoked).status()).toBe(200);
  await expect(grant).toContainText("已撤销");
  await expect(grant.getByRole("button", { name: "撤销", exact: true })).toHaveCount(0);
});

test("请求失败重试、成员接口权限与窄屏隔离", async ({ page, browser }, info) => {
  await login(page, "audit");
  await expect(page.locator(".organization-audit-table")).toBeVisible();
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await shot(page, info, "audit-offline-error");
  await page.context().setOffline(false);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.locator(".organization-audit-table")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await shot(page, info, "organization-mobile-inferred");
  const context = await browser.newContext();
  const member = await context.newPage();
  await login(member, "users", "dev@example.test");
  await expect(member.locator(".organization-members")).toHaveCount(0);
  for (const endpoint of ["/memberships", "/quotas", "/audit", "/support-grants"]) expect((await request(member, endpoint)).status).toBe(403);
  await context.close();
});
