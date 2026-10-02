import { test, expect } from "@playwright/test";
import type { BrowserContext, Page, TestInfo } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// All HTTP responses come from the unmodified API and migrated disposable DB.
// Enable only with workspace_acceptance_server.py and a local Vite proxy.
test.skip(process.env.PLATFORM_WORKSPACE_ACCEPTANCE !== "1", "Requires disposable workspace acceptance fixtures");
test.describe.configure({ mode: "serial" });
const password = "Agent-ui-acceptance-2026";
const fixtureDirectory = resolve(process.env.PLATFORM_WORKSPACE_ACCEPTANCE_DIRECTORY ?? ".design-to-ui/runs/20261003-node10-103/acceptance-server");
const python = process.env.PLATFORM_ACCEPTANCE_PYTHON ?? resolve("../../../.venv/bin/python");
const authenticatedCookies = new Map<string, Awaited<ReturnType<BrowserContext["cookies"]>>>();

/** Real DB fixture maintenance, restricted to the separate pagination tenant. */
function paginationFixture(operation: "restore" | "overlap") {
  const marker = JSON.parse(readFileSync(resolve(fixtureDirectory, "fixtures-ready.json"), "utf8"));
  expect(marker.pagination_tenant_id).toBe("acceptance_pagination_tenant");
  const script = `
import sqlite3, sys
from datetime import datetime, timedelta, timezone
db = sqlite3.connect(sys.argv[1])
scope = 'acceptance_pagination_tenant'
assert db.execute('SELECT name FROM platform_tenants WHERE id=?', (scope,)).fetchone() == ('分页验收空间',)
with db:
    if sys.argv[2] == 'overlap':
        db.execute('UPDATE platform_runs SET created_at=? WHERE tenant_id=? AND id=?', ('2026-09-25T04:09:30Z',scope,'run_page_059'))
    else:
        for index in range(60):
            status = 'running' if index == 59 else 'queued' if index == 58 else 'succeeded'
            created = (datetime(2026,9,25,4,tzinfo=timezone.utc)+timedelta(minutes=index)).isoformat().replace('+00:00','Z')
            db.execute('UPDATE platform_runs SET created_at=?,status=?,cancel_requested=0,finished_at=? WHERE tenant_id=? AND id=?', (created,status,created if status=='succeeded' else None,scope,f'run_page_{index:03d}'))
        db.execute('DELETE FROM platform_run_events WHERE tenant_id=? AND sequence>1', (scope,))
db.close()
`;
  execFileSync(python, ["-c", script, resolve(fixtureDirectory, "acceptance.db"), operation]);
}

test.beforeAll(() => paginationFixture("restore"));
test.afterAll(() => paginationFixture("restore"));

async function login(page: Page, email = "admin@example.test", route = "#overview") {
  const cookies = authenticatedCookies.get(email);
  if (cookies) await page.context().addCookies(cookies);
  await page.goto(`/${route}`);
  if (!cookies) {
    await page.getByLabel("工作邮箱").fill(email);
    await page.getByLabel("密码", { exact: true }).fill(password);
    await page.getByRole("button", { name: "进入工作空间" }).click();
  }
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
  expect((await page.context().cookies()).some(cookie => cookie.httpOnly)).toBe(true);
  authenticatedCookies.set(email, await page.context().cookies());
}

async function api(page: Page, path: string, method = "GET", body?: unknown) {
  return page.evaluate(async ({ path, method, body }) => {
    const tenant = sessionStorage.getItem("agent-platform.tenant");
    const identity = await fetch("/api/v2/me", { credentials: "same-origin" }).then(response => response.json());
    const response = await fetch(`/api/v2/tenants/${tenant}${path}`, {
      method, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": identity.csrf_token },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    return { status: response.status, body: await response.json() };
  }, { path, method, body });
}

async function screenshot(page: Page, info: TestInfo, name: string) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(Array.from(document.images).map(image => image.decode().catch(() => undefined)));
  });
  await page.screenshot({ path: info.outputPath(`${name}.png`), animations: "disabled", caret: "hide" });
}

const rows = (page: Page) => page.getByRole("table").locator("tbody tr");
const rowFor = (page: Page, id: string) => rows(page).filter({ hasText: id });
async function detail(page: Page, id: string) {
  const response = page.waitForResponse(response => response.request().method() === "GET" && new URL(response.url()).pathname.endsWith(`/runs/${id}`));
  await rowFor(page, id).getByRole("button", { name: "查看详情", exact: true }).click();
  expect((await response).status()).toBe(200);
  const dialog = page.getByRole("dialog", { name: "运行详情" });
  await expect(dialog).toContainText(id);
  return dialog;
}

test("组织概览真实聚合、曲线/模型事实与私有最近运行", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 1280, height: 1024 });
  await login(page);
  const overview = page.getByTestId("workspace-overview");
  await expect(overview).toBeVisible();
  await expect(overview).toContainText("当前组织调用概况与近期执行状态");
  const dashboard = await api(page, "/dashboard");
  expect(dashboard.status).toBe(200);
  expect(dashboard.body).toMatchObject({ requests: 1280, tokens: 1284500, cost: "12.4800", success_rate: 96.4, active_runs: 2 });
  expect(dashboard.body.daily.map((day: { requests: number }) => day.requests)).toEqual([110, 180, 148, 235, 265, 210, 132]);
  expect(dashboard.body.models.map((model: { requests: number }) => model.requests)).toEqual([896, 384]);
  for (const value of ["1,280", "1,284,500", "$12.4800", "96.4%", "gpt-4o", "claude-3-5-sonnet"]) await expect(overview).toContainText(value);
  await expect(overview.locator("section").filter({ hasText: "活跃运行 (Active)" }).locator("strong")).toHaveText("2");
  for (const run of ["run_demo_003", "run_demo_002", "run_demo_001"]) expect((await api(page, `/runs/${run}`)).status).toBe(200);
  await expect(overview).not.toContainText("run_member_active");
  expect((await api(page, "/runs/run_member_active")).status).toBe(404);
  await screenshot(page, info, "overview-admin-real-api");
  const chart = overview.getByRole("img", { name: "每日调用趋势，最近最多30个有调用的日期", exact: true });
  const descriptionId = await chart.getAttribute("aria-describedby");
  const readableData = overview.locator(`[id="${descriptionId}"]`);
  for (const day of dashboard.body.daily) {
    await expect(readableData).toContainText(`${day.date}：${day.requests} 次调用`);
  }
  const points = (await chart.locator("polyline").getAttribute("points"))!.split(" ").map(point => point.split(",").map(Number));
  expect(points).toHaveLength(dashboard.body.daily.length);
  expect(points.findIndex(point => point[1] === Math.min(...points.map(point => point[1])))).toBe(4);
  const distribution = overview.getByRole("table", { name: "模型分布", exact: true });
  for (const model of dashboard.body.models) await expect(distribution.getByRole("row").filter({ hasText: model.model })).toContainText(String(model.requests));
  await overview.getByRole("button", { name: "开始对话", exact: true }).click();
  await expect(page).toHaveURL(/#playground/);
  expect(errors).toEqual([]);
});

test("成员概览仅本人用量，缺少钱包保留未知值", async ({ page }, info) => {
  await login(page, "member@example.test");
  const overview = page.getByTestId("workspace-overview");
  await expect(overview).toContainText("我的调用概况与近期执行状态");
  const dashboard = await api(page, "/dashboard");
  expect(dashboard.body.requests).toBe(1277);
  expect(dashboard.body.tokens).toBe(1284050);
  expect(dashboard.body.cost).toBe("12.4725");
  expect(dashboard.body.active_runs).toBe(1);
  expect(dashboard.body).not.toHaveProperty("available");
  expect(dashboard.body).not.toHaveProperty("balance");
  await expect(overview).toContainText("1,277");
  await expect(overview).toContainText("1,284,050");
  await expect(overview).toContainText("$12.4725");
  await expect(overview).not.toContainText("可用余额");
  await expect(overview.locator("section").filter({ hasText: "活跃运行 (Active)" }).locator("strong")).toHaveText("1");
  await screenshot(page, info, "overview-member-real-api");
  await page.getByRole("link", { name: "我的运行记录", exact: true }).click();
  await expect(rows(page)).toHaveCount(2);
  await expect(page.getByRole("table")).not.toContainText("run_demo_");
  expect((await api(page, "/runs/run_demo_003")).status).toBe(404);
});

test("本人列表、纯本地筛选、真实详情读取与搜索空态", async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 1024 });
  await login(page, undefined, "#runs");
  await expect(page.getByRole("heading", { name: "我的运行记录", exact: true })).toBeVisible();
  await expect(rows(page)).toHaveCount(3);
  await expect(rowFor(page, "run_demo_003")).toContainText("$0.0021");
  await expect(rowFor(page, "run_demo_003")).toContainText("10-02 14:20");
  await expect(rowFor(page, "run_demo_002")).toContainText("claude-3-5-sonnet");
  await expect(rowFor(page, "run_demo_001")).toContainText("已取消");
  await expect(page.getByRole("button", { name: "加载更多", exact: true })).toBeDisabled();
  await screenshot(page, info, "runs-admin-real-api");
  let listRequests = 0;
  page.on("request", request => { if (request.method() === "GET" && new URL(request.url()).pathname.endsWith("/runs")) listRequests++; });
  const search = page.getByRole("textbox", { name: "筛选已加载的运行记录" });
  await search.fill("claude-3-5-sonnet");
  await expect(rows(page)).toHaveCount(1);
  await search.fill("RUN_DEMO_003");
  await expect(rows(page)).toHaveCount(1);
  await search.fill("不存在的记录");
  await expect(page.getByText("当前已加载记录无匹配", { exact: true })).toBeVisible();
  await search.fill("");
  expect(listRequests).toBe(0);
  const dialog = await detail(page, "run_demo_002");
  await expect(dialog).toContainText("验收输入 run_demo_002");
  await expect(dialog).toContainText("$0.0048");
  await expect(dialog.getByRole("button", { name: "取消运行", exact: true })).toHaveCount(0);
  await screenshot(page, info, "run-detail-real-api");
  await dialog.getByRole("button", { name: "查看会话", exact: true }).click();
  await expect(page).toHaveURL(/#playground\?.*session=session_run_demo_002/);
});

test("真实cursor追加、断网重试、按ID去重与刷新复位", async ({ page }, info) => {
  paginationFixture("restore");
  await login(page, "pagination@example.test", "#runs");
  await expect(rows(page)).toHaveCount(50);
  await expect(page.getByText("已加载 50 条", { exact: true })).toBeVisible();
  const first = await api(page, "/runs?limit=50");
  expect(first.body.items).toHaveLength(50);
  expect(first.body.next_cursor).toBeTruthy();
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "加载更多", exact: true }).click();
  const alert = page.getByRole("alert");
  await expect(alert).toContainText("暂时无法连接服务");
  await expect(rows(page)).toHaveCount(50);
  await screenshot(page, info, "runs-append-offline-preserves-rows");
  await page.context().setOffline(false);
  // A real record moves behind the cursor while the first page is displayed.
  // The API returns that overlapping ID; the UI must append each ID once.
  paginationFixture("overlap");
  const appended = page.waitForResponse(response => response.request().method() === "GET" && new URL(response.url()).pathname.endsWith("/runs") && new URL(response.url()).searchParams.has("cursor"));
  await alert.getByRole("button", { name: "重试", exact: true }).click();
  const next = await (await appended).json();
  expect(next.items).toHaveLength(11);
  expect(next.items.some((run: { id: string }) => run.id === "run_page_059")).toBe(true);
  await expect(rows(page)).toHaveCount(60);
  const ids = await rows(page).evaluateAll(elements => elements.map(row => row.querySelector("td span[title]")?.getAttribute("title")));
  expect(ids).toHaveLength(60);
  expect(new Set(ids).size).toBe(60);
  await expect(page.getByRole("button", { name: "加载更多", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(rows(page)).toHaveCount(50);
  await expect(page.getByText("已加载 50 条", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "加载更多", exact: true })).toBeEnabled();
  paginationFixture("restore");
});

test("queued真实取消到cancelled，running真实取消到cancelling", async ({ page }, info) => {
  paginationFixture("restore");
  await login(page, "pagination@example.test", "#runs");
  let dialog = await detail(page, "run_page_058");
  const assertDetailsPreserved = async (id: string) => {
    // Read the rendered state immediately, so a later five-second GET poll
    // cannot hide a momentary loss of fields after the cancellation response.
    const snapshot = await dialog.evaluate(element => ({
      text: element.textContent ?? "",
      cost: element.querySelector('dd[title="0 USD"]')?.textContent ?? "",
    }));
    expect(snapshot.text).toContain("gpt-4o");
    expect(snapshot.cost).toContain("$0.00");
    expect(snapshot.text).toContain(`验收输入 ${id}`);
  };
  expect((await api(page, "/runs/run_page_058")).body).toMatchObject({ model_alias: "gpt-4o", cost: "0", message: "验收输入 run_page_058" });
  await assertDetailsPreserved("run_page_058");
  const queuedCancel = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/runs/run_page_058/cancel"));
  await dialog.getByRole("button", { name: "取消运行", exact: true }).click();
  expect((await (await queuedCancel).json()).status).toBe("cancelled");
  await expect(dialog).toContainText("已取消");
  await expect(dialog.getByRole("button", { name: "取消运行", exact: true })).toHaveCount(0);
  await expect(rowFor(page, "run_page_058")).toContainText("已取消");
  await assertDetailsPreserved("run_page_058");
  await screenshot(page, info, "run-cancelled-detail-preserved");
  await page.keyboard.press("Escape");
  dialog = await detail(page, "run_page_059");
  expect((await api(page, "/runs/run_page_059")).body).toMatchObject({ model_alias: "gpt-4o", cost: "0", message: "验收输入 run_page_059" });
  await assertDetailsPreserved("run_page_059");
  await page.context().setOffline(true);
  await dialog.getByRole("button", { name: "取消运行", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("暂时无法连接服务");
  await expect(dialog.getByRole("button", { name: "取消运行", exact: true })).toBeEnabled();
  await page.context().setOffline(false);
  const runningCancel = page.waitForResponse(response => response.request().method() === "POST" && new URL(response.url()).pathname.endsWith("/runs/run_page_059/cancel"));
  await dialog.getByRole("button", { name: "取消运行", exact: true }).click();
  expect((await (await runningCancel).json()).status).toBe("cancelling");
  await expect(dialog.getByRole("button", { name: "取消中", exact: true })).toBeDisabled();
  await expect(dialog.getByRole("status")).toContainText("等待运行停止");
  await expect(rowFor(page, "run_page_059")).toContainText("取消中");
  await assertDetailsPreserved("run_page_059");
  expect((await api(page, "/runs/run_page_059")).body.status).toBe("cancelling");
  await screenshot(page, info, "run-cancelling-real-api");
});

test("真实列表断网失败、重试恢复，概览断网失败与空态", async ({ page }, info) => {
  await login(page, "empty@example.test", "#runs");
  await expect(page.getByText("暂无运行记录", { exact: true })).toBeVisible();
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await screenshot(page, info, "runs-list-offline");
  await page.context().setOffline(false);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByText("暂无运行记录", { exact: true })).toBeVisible();
  await page.context().setOffline(true);
  await page.getByRole("link", { name: "概览 Dashboard", exact: true }).click();
  const overview = page.getByTestId("workspace-overview");
  await expect(overview.getByRole("alert").first()).toContainText("暂时无法连接服务");
  await screenshot(page, info, "overview-offline");
  await page.context().setOffline(false);
  await overview.getByRole("button", { name: "重试", exact: true }).first().click();
  await expect(overview).toContainText("暂无调用记录");
  if (await overview.getByRole("button", { name: "重试", exact: true }).count()) await overview.getByRole("button", { name: "重试", exact: true }).first().click();
  await expect(overview.getByText("暂无运行记录", { exact: true })).toBeVisible();
  expect((await api(page, "/dashboard")).body).toMatchObject({ requests: 0, tokens: 0, cost: "0", success_rate: 0, active_runs: 0, daily: [], models: [] });
  await screenshot(page, info, "overview-empty-real-api");
});

test("财务角色无私有运行权限，真实API拒绝读取/取消", async ({ page }) => {
  await login(page, "finance@example.test", "#overview");
  await expect(page.getByRole("link", { name: "我的运行记录", exact: true })).toHaveCount(0);
  const dashboard = await api(page, "/dashboard");
  expect(dashboard.status).toBe(200);
  expect(dashboard.body.requests).toBe(1280);
  expect((await api(page, "/runs")).status).toBe(403);
  expect((await api(page, "/runs/run_demo_003")).status).toBe(403);
  expect((await api(page, "/runs/run_demo_003/cancel", "POST")).status).toBe(403);
});

test("移动无溢出、跨工作空间/Agent路由侧栏状态保留、只Logo展开", async ({ page }, info) => {
  await page.setViewportSize({ width: 1280, height: 1024 });
  await login(page, undefined, "#runs");
  await page.getByRole("button", { name: "收起侧栏", exact: true }).click();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  await expect(page.getByRole("button", { name: "展开侧栏", exact: true })).toHaveCount(1);
  await expect(page.getByRole("button", { name: "展开侧栏", exact: true })).toHaveText("Z");
  await page.getByRole("link", { name: "概览 Dashboard", exact: true }).click();
  await expect(page.getByTestId("workspace-overview")).toBeVisible();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  await page.getByRole("link", { name: /^Agent\s*管理$/ }).click();
  await expect(page.getByTestId("agent-list")).toBeVisible();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  await expect(page.locator(".agent-table tbody tr")).toHaveCount(3);
  await expect(page.locator(".agent-collapse")).toHaveCount(0);
  const logo = page.getByRole("button", { name: "展开侧栏", exact: true });
  await expect(logo).not.toHaveAttribute("title");
  await logo.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".sidebar")).toHaveCSS("width", "240px");
  await page.getByRole("link", { name: "我的运行记录", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(rows(page)).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await screenshot(page, info, "runs-mobile-inferred");
  await page.getByRole("button", { name: "打开导航", exact: true }).click();
  await page.getByRole("link", { name: "概览 Dashboard", exact: true }).click();
  const mobileOverview = page.getByTestId("workspace-overview");
  await expect(mobileOverview).toContainText("1,284,500");
  await expect(mobileOverview.getByRole("img", { name: "每日调用趋势，最近最多30个有调用的日期", exact: true })).toBeVisible();
  await expect(mobileOverview.getByRole("table", { name: "我的近期运行", exact: true }).locator("tbody tr")).toHaveCount(3);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await screenshot(page, info, "overview-mobile-inferred");
});
