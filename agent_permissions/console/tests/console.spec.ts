import { expect, test, type Page } from "@playwright/test";

const adminPassword = "12345";
const initialPassword = "12345";

async function login(
  page: Page,
  username = "console_admin",
  password = adminPassword,
) {
  await page.goto("/console/#users");
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录控制台" }).click();
}

test("管理员使用 12345 登录后直接进入用户管理", async ({ page }) => {
  await login(page);
  await expect(
    page.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "设置你的新密码" }),
  ).toHaveCount(0);
  await expect(page.getByRole("link", { name: "角色与权限" })).toBeVisible();
});

test("成员首次登录直接进入个人设置，可主动修改简单密码", async ({ page }) => {
  await login(page, "first_login", initialPassword);
  await expect(
    page.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "用户管理", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("当前密码", { exact: true }).fill(initialPassword);
  await page.getByLabel("新密码", { exact: true }).fill(initialPassword);
  await page.getByLabel("确认新密码", { exact: true }).fill("different");
  await page.getByRole("button", { name: "更新密码" }).click();
  await expect(page.getByRole("alert")).toContainText("两次输入的新密码不一致");
  await page.getByLabel("确认新密码", { exact: true }).fill(initialPassword);
  await page.getByRole("button", { name: "更新密码" }).click();
  await expect(page.getByRole("status")).toContainText("密码已更新");
  await login(page, "first_login", initialPassword);
  await expect(
    page.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "角色与权限" })).toHaveCount(0);
});

test("真实 API 完成创建、编辑、角色、重置密码、启停和删除", async ({
  page,
}) => {
  const resetPassword = "Browser reset " + "long password ".repeat(20);
  await login(page);
  await expect(
    page.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新增用户", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("用户名", { exact: true }).fill("browser_created");
  await dialog.getByLabel("展示名称", { exact: true }).fill("浏览器新增用户");
  await dialog.getByLabel("联系邮箱（选填）").fill("browser@example.com");
  await dialog.getByLabel("初始密码", { exact: true }).fill(initialPassword);
  await dialog.getByRole("button", { name: "创建用户" }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("textbox", { name: "搜索用户" }).fill("browser_created");
  let row = page.getByRole("row").filter({ hasText: "browser_created" });
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: "编辑用户：browser_created" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("展示名称").fill("浏览器已编辑用户");
  await dialog.getByRole("button", { name: "保存更改" }).click();
  await expect(row).toContainText("浏览器已编辑用户");
  await row.getByRole("button", { name: "更多操作：browser_created" }).click();
  await page.getByRole("menuitem", { name: "设置角色" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /管理员/ }).check();
  await dialog.getByRole("button", { name: "保存更改" }).click();
  await expect(dialog).not.toBeVisible();
  await expect(row).toContainText("管理员");
  await row.getByRole("button", { name: "更多操作：browser_created" }).click();
  await page.getByRole("menuitem", { name: "重置密码" }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("新密码", { exact: true }).fill(resetPassword);
  await dialog.getByLabel("确认新密码", { exact: true }).fill(resetPassword);
  await dialog.getByRole("button", { name: "重置密码", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const resetPage = await page.context().newPage();
  await login(resetPage, "browser_created", resetPassword);
  await expect(
    resetPage.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  await resetPage.close();
  await row.getByRole("button", { name: "更多操作：browser_created" }).click();
  await page.getByRole("menuitem", { name: "禁用账号" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认禁用" })
    .click();
  await expect(row).toContainText("已禁用");
  await row.getByRole("button", { name: "更多操作：browser_created" }).click();
  await page.getByRole("menuitem", { name: "启用账号" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认启用" })
    .click();
  await expect(row).toContainText("已启用");
  await row.getByRole("button", { name: "更多操作：browser_created" }).click();
  await page.getByRole("menuitem", { name: "删除用户" }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认删除" })
    .click();
  await expect(
    page.getByRole("heading", { name: "没有找到符合条件的用户" }),
  ).toBeVisible();
});

test("版本冲突保留表单，只有主动载入才替换为最新资料", async ({
  page,
  request,
}) => {
  const authentication = await request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: adminPassword },
  });
  const token = (await authentication.json()).data.session_token;
  const headers = { Authorization: `Bearer ${token}` };
  const created = await request.post("/api/v1/users", {
    headers,
    data: {
      username: "conflict_user",
      display_name: "冲突测试用户",
      password: initialPassword,
    },
  });
  const id = (await created.json()).data.id;
  await login(page);
  await page.getByRole("textbox", { name: "搜索用户" }).fill("conflict_user");
  await page.getByRole("button", { name: "编辑用户：conflict_user" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("展示名称").fill("尚未提交的名称");
  const current = await request.get(`/api/v1/users/${id}`, { headers });
  await request.patch(`/api/v1/users/${id}`, {
    headers: { ...headers, "If-Match": current.headers().etag || "" },
    data: { display_name: "其他管理员更新的名称" },
  });
  await dialog.getByRole("button", { name: "保存更改" }).click();
  await expect(dialog.getByRole("alert")).toContainText("已被其他操作更新");
  await expect(dialog.getByLabel("展示名称")).toHaveValue("尚未提交的名称");
  await dialog.getByRole("button", { name: "载入最新信息" }).click();
  await expect(dialog.getByLabel("展示名称")).toHaveValue(
    "其他管理员更新的名称",
  );
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
});

test("成员只可管理自己，角色说明与个人资料页面可用", async ({ page }) => {
  await login(page, "console_member", "Console member password 2026!");
  await expect(
    page.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "用户管理", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("展示名称").fill("普通成员已更新");
  await page.getByRole("button", { name: "保存资料" }).click();
  await expect(
    page.getByRole("heading", { name: "普通成员已更新", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/console-profile.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await login(page);
  await page.getByRole("link", { name: "角色与权限", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "角色与权限", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "管理员", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("新增用户", { exact: true })).toBeVisible();
  await page.screenshot({
    path: "test-results/console-roles.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("查询失败可以重试，失效会话会回到登录页", async ({ page }) => {
  await login(page);
  await expect(
    page.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  let fail = true;
  await page.route("**/api/v1/users?*", async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: {
            code: "SERVICE_UNAVAILABLE",
            message: "暂时无法完成请求，请稍后重试",
            details: [],
          },
          request_id: "browser-failure",
        }),
      });
    else await route.continue();
  });
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法完成请求");
  fail = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByRole("alert")).not.toBeVisible();
  await page.unroute("**/api/v1/users?*");
  await page.route("**/api/v1/users?*", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "UNAUTHENTICATED", message: "请重新登录", details: [] },
        request_id: "browser-expired",
      }),
    }),
  );
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "登录 Agent Console" }),
  ).toBeVisible();
  await expect(page.getByRole("status")).toContainText("登录状态已失效");
});

for (const status of [200, 401]) {
  test(`旧会话延迟返回 ${status} 不影响新用户的登录`, async ({
    page,
    request,
  }) => {
    const authentication = await request.post("/api/v1/auth/login", {
      data: { username: "console_admin", password: adminPassword },
    });
    const oldUser = (await authentication.json()).data.user;
    await login(page);
    await page.getByRole("link", { name: "个人设置", exact: true }).click();
    await page.getByLabel("展示名称").fill("旧会话中的编辑");
    let release = () => {};
    let received = () => {};
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const entered = new Promise<void>((resolve) => {
      received = resolve;
    });
    await page.route("**/api/v1/users/me", async (route) => {
      if (route.request().method() !== "PATCH") {
        await route.continue();
        return;
      }
      received();
      await gate;
      await route.fulfill({
        status,
        contentType: "application/json",
        headers: { ETag: `"v${oldUser.version + 1}"` },
        body: JSON.stringify(
          status === 401
            ? {
                error: {
                  code: "UNAUTHENTICATED",
                  message: "请重新登录",
                  details: [],
                },
                request_id: "late-response",
              }
            : {
                data: {
                  ...oldUser,
                  display_name: "旧会话返回的资料",
                  version: oldUser.version + 1,
                },
                request_id: "late-response",
              },
        ),
      });
    });
    const lateResponse = page.waitForResponse(
      (response) =>
        response.url().endsWith("/api/v1/users/me") &&
        response.request().method() === "PATCH",
    );
    await page.getByRole("button", { name: "保存资料" }).click();
    await entered;
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "登录 Agent Console" }),
    ).toBeVisible();
    await login(page, "console_member", "Console member password 2026!");
    await expect(
      page.getByRole("heading", { name: "个人设置", exact: true }),
    ).toBeVisible();
    release();
    await lateResponse;
    await page.waitForLoadState("networkidle");
    await expect(
      page.getByRole("heading", { name: "个人设置", exact: true }),
    ).toBeVisible();
    await expect(page.getByLabel("用户名", { exact: true })).toHaveValue(
      "console_member",
    );
    await expect(
      page.getByRole("link", { name: "用户管理", exact: true }),
    ).toHaveCount(0);
  });
}

test("可配置 RBAC 完成角色授权、多角色分配、撤销与删除", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "角色与权限", exact: true }).click();
  await page.getByRole("button", { name: "新增角色", exact: true }).click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("角色编码", { exact: true }).fill("browser_auditor");
  await dialog.getByLabel("角色名称", { exact: true }).fill("浏览器审计员");
  await dialog.getByLabel("角色说明", { exact: true }).fill("只读查看系统用户");
  await dialog.getByRole("checkbox", { name: /查询系统用户/ }).check();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/console-rbac-role-dialog.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByRole("button", { name: "创建角色", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const card = page.getByRole("region", {
    name: "角色：浏览器审计员",
    exact: true,
  });
  await expect(card).toContainText("4 项权限");
  await page.screenshot({
    path: "test-results/console-rbac-roles.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("link", { name: "用户管理", exact: true }).click();
  await page.getByRole("button", { name: "新增用户", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("用户名", { exact: true }).fill("rbac_browser");
  await dialog.getByLabel("展示名称", { exact: true }).fill("审计用户");
  await dialog.getByLabel("初始密码", { exact: true }).fill("12345");
  await dialog.getByRole("checkbox", { name: /浏览器审计员/ }).check();
  await dialog.getByRole("button", { name: "创建用户", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("textbox", { name: "搜索用户" }).fill("rbac_browser");
  const row = page.getByRole("row").filter({ hasText: "rbac_browser" });
  await expect(row).toContainText("浏览器审计员");
  await expect(row).toContainText("普通用户");
  await page.getByLabel("角色筛选").selectOption("browser_auditor");
  await expect(row).toBeVisible();
  const reader = await page.context().newPage();
  await login(reader, "rbac_browser", "12345");
  await expect(
    reader.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  await expect(
    reader.getByRole("button", { name: "新增用户", exact: true }),
  ).toHaveCount(0);
  await expect(reader.getByRole("button", { name: /编辑用户：/ })).toHaveCount(
    0,
  );
  await expect(
    reader.getByRole("link", { name: "角色与权限", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("link", { name: "角色与权限", exact: true }).click();
  await expect(
    card.getByRole("button", { name: "删除角色", exact: true }),
  ).toBeDisabled();
  await card.getByRole("button", { name: "编辑角色", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /查询系统用户/ }).uncheck();
  await dialog.getByRole("button", { name: "保存角色", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await reader.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(
    reader.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(
    reader.getByRole("link", { name: "用户管理", exact: true }),
  ).toHaveCount(0);
  await reader.close();
  await page.getByRole("link", { name: "用户管理", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索用户" }).fill("rbac_browser");
  await row.getByRole("button", { name: "更多操作：rbac_browser" }).click();
  await page.getByRole("menuitem", { name: "设置角色", exact: true }).click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("checkbox", { name: /浏览器审计员/ }).uncheck();
  await dialog.getByRole("button", { name: "保存更改", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page.getByRole("link", { name: "角色与权限", exact: true }).click();
  await expect(
    card.getByRole("button", { name: "删除角色", exact: true }),
  ).toBeEnabled();
  await card.getByRole("button", { name: "删除角色", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认删除", exact: true })
    .click();
  await expect(card).toHaveCount(0);
});

test("角色编辑冲突保留草稿，并支持主动载入最新定义", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "角色与权限", exact: true }).click();
  await page.getByRole("button", { name: "新增角色", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog
    .getByLabel("角色编码", { exact: true })
    .fill("browser_conflict_role");
  await dialog.getByLabel("角色名称", { exact: true }).fill("待编辑角色");
  await dialog.getByRole("button", { name: "创建角色", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await page
    .getByRole("region", { name: "角色：待编辑角色", exact: true })
    .getByRole("button", { name: "编辑角色", exact: true })
    .click();
  await dialog.getByLabel("角色名称", { exact: true }).fill("角色草稿");
  const loginResponse = await page.request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: adminPassword },
  });
  const token = (await loginResponse.json()).data.session_token;
  const headers = { Authorization: `Bearer ${token}` };
  const current = await page.request.get(
    "/api/v1/roles/browser_conflict_role",
    { headers },
  );
  const etag = current.headers().etag;
  if (!etag) throw new Error("角色详情缺少 ETag");
  const updated = await page.request.patch(
    "/api/v1/roles/browser_conflict_role",
    {
      headers: { ...headers, "If-Match": etag },
      data: { display_name: "其他管理员更新的角色" },
    },
  );
  expect(updated.ok()).toBe(true);
  await dialog.getByRole("button", { name: "保存角色", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("信息已被其他操作更新");
  await expect(dialog.getByLabel("角色名称", { exact: true })).toHaveValue(
    "角色草稿",
  );
  await dialog
    .getByRole("button", { name: "载入最新信息", exact: true })
    .click();
  await expect(dialog.getByLabel("角色名称", { exact: true })).toHaveValue(
    "其他管理员更新的角色",
  );
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
});

test("没有 Console 基础权限的角色显示拒绝状态且不循环刷新", async ({
  page,
}) => {
  const auth = await page.request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: adminPassword },
  });
  const headers = {
    Authorization: `Bearer ${(await auth.json()).data.session_token}`,
  };
  const role = await page.request.post("/api/v1/roles", {
    headers,
    data: {
      code: "browser_no_access",
      display_name: "无访问权限",
      permissions: [],
    },
  });
  expect(role.ok()).toBe(true);
  const user = await page.request.post("/api/v1/users", {
    headers,
    data: {
      username: "browser_no_access",
      display_name: "无权限用户",
      password: "12345",
      roles: ["browser_no_access"],
    },
  });
  expect(user.ok()).toBe(true);
  let identityReads = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/users/me")
      identityReads += 1;
  });
  await login(page, "browser_no_access", "12345");
  await expect(
    page.getByRole("button", { name: "返回登录", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("没有执行该操作的权限");
  expect(identityReads).toBe(1);
});

test("桌面与窄屏布局，以及表单的键盘取消", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/console/");
  await page.screenshot({
    path: "test-results/console-login.png",
    fullPage: true,
    animations: "disabled",
  });
  await login(page);
  await expect(
    page.getByRole("row").filter({ hasText: "console_admin" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/console-users.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "新增用户", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).not.toBeVisible();
  await expect(
    page.getByRole("button", { name: "新增用户", exact: true }),
  ).toBeFocused();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/console-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.getByRole("button", { name: "新增用户", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("用户名", { exact: true })).toBeVisible();
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  if (bounds) {
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(390);
  }
  await page.screenshot({
    path: "test-results/console-mobile-dialog.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 320, height: 720 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.getByRole("button", { name: "更多操作：console_admin" }).click();
  const menu = page.getByRole("menu");
  await expect(menu).toBeVisible();
  await expect(menu).toHaveCSS("animation-name", "none");
  await page.keyboard.press("Escape");
  await expect(menu).not.toBeVisible();
  expect(errors).toEqual([]);
});
