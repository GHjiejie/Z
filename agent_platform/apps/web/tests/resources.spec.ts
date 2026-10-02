import { test, expect } from "@playwright/test";
import type { Page, TestInfo } from "@playwright/test";

test.skip(process.env.PLATFORM_RESOURCES_ACCEPTANCE !== "1", "Requires a fresh resources_acceptance_server.py database");
test.describe.configure({ mode: "serial" });
test.use({ viewport: { width: 1280, height: 1024 } });

async function login(page: Page, route: string, email = "admin@example.test") {
  await page.goto(`/#${route}`);
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill("Agent-ui-acceptance-2026");
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
}
async function request(page: Page, path: string, method = "GET", body?: unknown) {
  return page.evaluate(async ({ path, method, body }) => {
    const identity = await fetch("/api/v2/me").then(response => response.json());
    const tenant = sessionStorage.getItem("agent-platform.tenant");
    const response = await fetch(`/api/v2/tenants/${tenant}${path}`, { method, credentials: "same-origin", headers: { "Content-Type": "application/json", "X-CSRF-Token": identity.csrf_token }, body: body === undefined ? undefined : JSON.stringify(body) });
    return { status: response.status, body: await response.json() };
  }, { path, method, body });
}
async function shot(page: Page, info: TestInfo, name: string) {
  await page.evaluate(async () => { await document.fonts.ready; await Promise.all(Array.from(document.images).map(image => image.decode())); });
  await page.screenshot({ path: info.outputPath(`${name}.png`), animations: "disabled", caret: "hide" });
}

test("真实策略保存、版本冲突反馈与恢复", async ({ page }, info) => {
  await login(page, "models");
  const original = (await request(page, "/model-policy")).body;
  await page.getByRole("button", { name: "编辑策略", exact: true }).click();
  let dialog = page.getByRole("dialog", { name: "组织模型选择策略" });
  await shot(page, info, "model-policy-inferred");
  await dialog.getByLabel("组织默认模型", { exact: true }).selectOption("model_claude_acceptance");
  const saved = page.waitForResponse(response => response.request().method() === "PUT" && response.url().endsWith("/model-policy"));
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await saved).status()).toBe(200);
  await expect(page.locator(".resource-default code")).toHaveText("claude-3-5-sonnet");
  const current = (await request(page, "/model-policy")).body;
  expect(current.version).toBe(original.version + 1);
  await page.getByRole("button", { name: "编辑策略", exact: true }).click();
  dialog = page.getByRole("dialog", { name: "组织模型选择策略" });
  expect((await request(page, "/model-policy", "PUT", { default_model_id: original.default_model_id, ordered_model_ids: original.ordered_model_ids, expected_version: current.version })).status).toBe(200);
  const conflict = page.waitForResponse(response => response.request().method() === "PUT" && response.url().endsWith("/model-policy"));
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await conflict).status()).toBe(409);
  await expect(dialog.getByRole("alert")).toBeVisible();
  await expect(dialog.getByLabel("组织默认模型", { exact: true })).toHaveValue("model_claude_acceptance");
  await shot(page, info, "policy-conflict-inferred");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
});

test("模型过滤、停用确认只修改 active，并恢复启用", async ({ page }, info) => {
  await login(page, "models");
  const search = page.getByLabel("筛选已加载模型...");
  await search.fill("claude");
  const row = page.locator(".resource-model-table tbody tr");
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "停用", exact: true }).click();
  await shot(page, info, "model-disable-confirmation-inferred");
  const patch = page.waitForRequest(req => req.method() === "PATCH" && req.url().includes("/models/"));
  await page.getByRole("dialog", { name: "停用模型" }).getByRole("button", { name: "确认", exact: true }).click();
  expect((await patch).postDataJSON()).toEqual({ active: false });
  await expect(row).toContainText("已停用");
  await row.getByRole("button", { name: "启用", exact: true }).click();
  await page.getByRole("dialog", { name: "启用模型" }).getByRole("button", { name: "确认", exact: true }).click();
  await expect(row).toContainText("已启用");
  const model = (await request(page, "/models")).body.items.find((item: { id: string }) => item.id === "model_claude_acceptance");
  expect(model.active).toBe(true);
  expect(Number(model.input_price)).toBe(3);
});

test("钱包、流水过滤、预占记录和真实刷新", async ({ page }, info) => {
  await login(page, "billing");
  await expect(page.locator(".resource-wallet-grid")).toContainText("$100.0000");
  await expect(page.locator(".resource-wallet-grid")).toContainText("$98.0000");
  await expect(page.locator(".resource-ledger-table tbody tr")).toHaveCount(3);
  await page.getByLabel("筛选流水说明...").fill("内容助手");
  await expect(page.locator(".resource-ledger-table tbody tr")).toHaveCount(1);
  await page.getByRole("tab", { name: /预占记录/ }).click();
  await expect(page.locator(".resource-table tbody tr")).toHaveCount(2);
  await expect(page.locator(".resource-table")).toContainText("待核实");
  await shot(page, info, "billing-reservations-inferred");
  const refresh = page.waitForResponse(response => response.url().endsWith("/billing/wallet"));
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  expect((await refresh).status()).toBe(200);
});

test("真实游标加载追加、调用元数据、筛选及当前 CSV", async ({ page }, info) => {
  await login(page, "usage");
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(100);
  await page.route("**/usage/calls?*cursor=**", route => route.abort("connectionfailed"));
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(100);
  await shot(page, info, "calls-pagination-failure-inferred");
  await page.unroute("**/usage/calls?*cursor=**");
  const next = page.waitForRequest(req => req.url().includes("/usage/calls?") && req.url().includes("cursor="));
  await page.getByRole("button", { name: "重试", exact: true }).click();
  expect((await next).url()).toContain("limit=100");
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(103);
  await expect(page.getByRole("button", { name: "加载更多", exact: true })).toBeDisabled();
  await page.getByLabel("筛选已加载调用...").fill("call_demo_002");
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "查看", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "调用详情" });
  await expect(dialog).toContainText("run_demo_002");
  await expect(dialog).toContainText("$0.0048");
  await shot(page, info, "call-detail-inferred");
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.locator(".resource-call-options summary").click();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出当前筛选结果 CSV" }).click();
  expect((await download).suggestedFilename()).toBe("agent-usage-current.csv");
  await page.getByLabel("筛选已加载调用...").fill("no-match");
  await expect(page.getByText("当前已加载记录无匹配", { exact: true })).toBeVisible();
});

test("网络失败保留输入并可重试恢复", async ({ page }, info) => {
  await page.route("**/usage/calls?**", route => route.abort("connectionfailed"));
  await login(page, "usage");
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await shot(page, info, "calls-request-failure-inferred");
  await page.unroute("**/usage/calls?**");
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(100);
});

test("导出下载、过期与准备中状态、真实申请和幂等键", async ({ page }, info) => {
  await login(page, "usage?view=exports");
  const ready = page.locator(".resource-export-table tbody tr").filter({ hasText: "export_demo_002" });
  const download = page.waitForEvent("download");
  const response = page.waitForResponse(response => response.url().includes("/exports/export_demo_002/download"));
  await ready.getByRole("button", { name: "下载 CSV", exact: true }).click();
  expect((await response).status()).toBe(200);
  expect((await download).suggestedFilename()).toBe("calls-export_demo_002.csv");
  await expect(page.locator("tr").filter({ hasText: "export_demo_001" }).getByRole("button", { name: "已过期", exact: true })).toBeDisabled();
  await expect(page.locator("tr").filter({ hasText: "export_demo_003" }).getByRole("button", { name: "准备中", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "创建导出", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "申请数据导出" });
  await dialog.getByLabel("数据类型", { exact: true }).selectOption("runs");
  await expect(dialog.getByLabel("数据范围", { exact: true }).locator("option")).toHaveCount(1);
  await shot(page, info, "export-create-inferred");
  await page.route("**/exports", route => route.request().method() === "POST" ? route.abort("connectionfailed") : route.continue());
  const failedRequest = page.waitForRequest(req => req.method() === "POST" && req.url().endsWith("/exports"));
  await dialog.getByRole("button", { name: "提交导出申请", exact: true }).click();
  const failedKey = (await failedRequest).headers()["idempotency-key"];
  await expect(dialog.getByRole("alert")).toContainText("暂时无法连接服务");
  await expect(dialog.getByLabel("数据类型", { exact: true })).toHaveValue("runs");
  await shot(page, info, "export-request-failure-inferred");
  await page.unroute("**/exports");
  const sent = page.waitForRequest(req => req.method() === "POST" && req.url().endsWith("/exports"));
  const accepted = page.waitForResponse(response => response.request().method() === "POST" && response.url().endsWith("/exports"));
  await dialog.getByRole("button", { name: "提交导出申请", exact: true }).click();
  const req = await sent;
  expect(req.postDataJSON()).toEqual({ kind: "runs", scope: "self" });
  expect(req.headers()["idempotency-key"]).toMatch(/^[0-9a-f-]{36}$/);
  expect(req.headers()["idempotency-key"]).toBe(failedKey);
  expect((await accepted).status()).toBe(202);
  await expect(dialog).toHaveCount(0);
  await expect(page.locator(".resource-export-table tbody tr")).toHaveCount(4);
});

test("member 只读模型、仅本人调用和导出隔离", async ({ page }) => {
  await login(page, "models", "member@example.test");
  await expect(page.locator(".resource-model-table tbody tr")).toHaveCount(2);
  await expect(page.getByRole("button", { name: "编辑策略", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "停用", exact: true })).toHaveCount(0);
  expect((await request(page, "/model-policy", "PUT", { default_model_id: null, ordered_model_ids: [], expected_version: 1 })).status).toBe(403);
  await page.goto("/#usage");
  await expect(page.getByText("暂无调用明细", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "我的导出", exact: true }).click();
  await expect(page.getByText("还没有导出文件", { exact: true })).toBeVisible();
  expect((await request(page, "/exports/export_demo_002/download")).status).toBe(404);
  await page.getByRole("button", { name: "创建导出", exact: true }).click();
  await expect(page.getByLabel("数据范围", { exact: true }).locator("option")).toHaveCount(1);
});

test("finance_viewer、侧栏 Logo 展开、资源样式作用域及移动布局", async ({ page }, info) => {
  await login(page, "billing", "finance@example.test");
  await expect(page.locator(".resource-wallet-grid")).toContainText("$100.0000");
  await expect(page.locator(".resource-nav-link[href*='models']")).toHaveCount(0);
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  const logo = page.getByRole("button", { name: "展开侧栏", exact: true });
  await expect(logo).toHaveText("Z");
  await expect(logo).not.toHaveAttribute("title");
  await expect(page.locator(".resource-collapse")).toHaveCount(0);
  await shot(page, info, "resources-collapsed-inferred");
  await logo.focus(); await page.keyboard.press("Enter");
  await expect(page.locator(".sidebar")).toHaveCSS("width", "240px");
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator(".resource-wallet-grid")).toHaveCSS("grid-template-columns", /\d+px/);
  await shot(page, info, "billing-mobile-inferred");
  await page.setViewportSize({ width: 1280, height: 1024 });
  await page.goto("/#usage");
  await expect(page.locator(".resource-call-table tbody tr")).toHaveCount(100);
});

test("调用页创建入口和 Agent 页资源样式隔离", async ({ page }, info) => {
  await login(page, "usage");
  await page.getByRole("button", { name: "创建导出", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "申请数据导出" })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page.goto("/#agents");
  await expect(page.locator(".agent-shell")).toBeVisible();
  await expect(page.locator(".resources-shell,.resource-page,.resource-nav")).toHaveCount(0);
  await shot(page, info, "agent-existing-style-scope");
});
