import { randomUUID } from "node:crypto";
import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

async function apiLogin(request: APIRequestContext, username: string) {
  const response = await request.post("/api/v1/auth/login", {
    data: { username, password: "12345" },
  });
  expect(response.ok()).toBeTruthy();
  const { data } = await response.json();
  return {
    userId: String(data.user.id),
    headers: { Authorization: `Bearer ${data.session_token}` },
  };
}

async function accountWithUsage(request: APIRequestContext) {
  const admin = await apiLogin(request, "console_admin");
  const username = `usage_${randomUUID().slice(0, 8)}`;
  const response = await request.post("/api/v1/users", {
    headers: admin.headers,
    data: { username, display_name: "用量测试成员", password: "12345" },
  });
  expect(response.status()).toBe(201);
  const account = await apiLogin(request, username);
  const completion = await request.post("/v1/chat/completions", {
    headers: account.headers,
    data: {
      model: "fixture-model",
      messages: [{ role: "user", content: "查看当前账号" }],
    },
  });
  expect(completion.ok()).toBeTruthy();
  const { usage } = await completion.json();
  expect(usage.total_tokens).toBeGreaterThan(0);
  return { username, account, admin, total: String(usage.total_tokens) };
}

async function login(page: Page, username: string, path: string) {
  await page.goto(path);
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "登录控制台" }).click();
}

test("管理员从侧栏按用户 ID 和日期查用量，也能从用户管理直达", async ({
  page,
  request,
}) => {
  const { username, account, total } = await accountWithUsage(request);
  await login(page, "console_admin", "/console/#users");
  const menu = page.getByRole("link", { name: "模型用量", exact: true });
  await menu.click();
  await expect(menu).toHaveAttribute("aria-current", "page");
  const query = page.getByRole("button", { name: "查询", exact: true });
  const id = page.getByLabel("用户 ID", { exact: true });
  await id.fill(account.userId);
  await query.click();
  const results = page.getByRole("region", { name: "用量统计结果" });
  const totalCard = results
    .locator(".stat-card")
    .filter({ hasText: "已确认总 Token" });
  await expect(
    results.getByText(account.userId, { exact: true }),
  ).toBeVisible();
  await expect(totalCard.locator("strong")).toHaveText(total);

  await page.getByLabel("开始日期", { exact: true }).fill("2000-01-02");
  await page.getByLabel("结束日期", { exact: true }).fill("2000-01-01");
  await query.click();
  await expect(page.getByRole("alert")).toContainText(
    "结束日期不能早于开始日期",
  );
  await page.getByLabel("开始日期", { exact: true }).fill("2000-01-01");
  await query.click();
  await expect(
    results.getByRole("heading", { name: "暂无用量记录" }),
  ).toBeVisible();

  await page.getByRole("link", { name: "用户管理", exact: true }).click();
  await page.getByRole("button", { name: `更多操作：${username}` }).click();
  await page
    .getByRole("menuitem", { name: "查看模型用量", exact: true })
    .click();
  await expect(page).toHaveURL(
    new RegExp(`#usage\\?user_id=${account.userId}$`),
  );
  await expect(id).toHaveValue(account.userId);
  await expect(totalCard.locator("strong")).toHaveText(total);
  await id.fill("missing_usage_account");
  await query.click();
  await expect(results.getByRole("alert")).toContainText("用户不存在或已删除");
});

test("普通账号在窄屏通过侧栏查看自己的用量", async ({ page, request }) => {
  const { username, account, admin, total } = await accountWithUsage(request);
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, username, "/console/#profile");
  await page.getByRole("link", { name: "模型用量", exact: true }).click();
  const results = page.getByRole("region", { name: "用量统计结果" });
  await expect(
    results.getByText(account.userId, { exact: true }),
  ).toBeVisible();
  await expect(results.getByText(total, { exact: true })).toBeVisible();
  await expect(page.getByLabel("用户 ID", { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);

  await page.evaluate((id) => {
    location.hash = `usage?user_id=${id}`;
  }, admin.userId);
  await expect(
    results.getByText(account.userId, { exact: true }),
  ).toBeVisible();
  await expect(results.getByText(admin.userId, { exact: true })).toHaveCount(0);
});
