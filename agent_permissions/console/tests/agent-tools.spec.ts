import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

interface Target {
  headers: { Authorization: string };
  role: string;
  username: string;
  id: string;
}
const createdTargets: Target[] = [];

test.afterEach(async ({ request }) => {
  for (const target of createdTargets.splice(0)) {
    const endpoint = `/api/v1/users/${target.id}`;
    const current = await request.get(endpoint, { headers: target.headers });
    const etag = current.headers().etag;
    if (!etag) throw new Error("测试清理缺少用户版本");
    const moved = await request.put(`${endpoint}/roles`, {
      headers: { ...target.headers, "If-Match": etag },
      data: { roles: ["member"] },
    });
    const latest = moved.headers().etag;
    if (!latest) throw new Error("测试清理缺少新用户版本");
    expect(
      (
        await request.delete(endpoint, {
          headers: { ...target.headers, "If-Match": latest },
        })
      ).status(),
    ).toBe(204);
    const roleEndpoint = `/api/v1/roles/${target.role}`;
    const role = await request.get(roleEndpoint, { headers: target.headers });
    const roleTag = role.headers().etag;
    if (!roleTag) throw new Error("测试清理缺少角色版本");
    expect(
      (
        await request.delete(roleEndpoint, {
          headers: { ...target.headers, "If-Match": roleTag },
        })
      ).status(),
    ).toBe(204);
  }
});

async function admin(request: APIRequestContext) {
  const response = await request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: "12345" },
  });
  expect(response.ok()).toBeTruthy();
  return {
    Authorization: `Bearer ${(await response.json()).data.session_token}`,
  };
}

async function createTarget(
  request: APIRequestContext,
  suffix: string,
  rolePermissions = ["user.self.read"],
) {
  const headers = await admin(request);
  const role = `grant_${suffix}`;
  const username = `grant_${suffix}`;
  expect(
    (
      await request.post("/api/v1/roles", {
        headers,
        data: {
          code: role,
          display_name: `授权测试${suffix}`,
          permissions: rolePermissions,
        },
      })
    ).ok(),
  ).toBeTruthy();
  const response = await request.post("/api/v1/users", {
    headers,
    data: {
      username,
      display_name: `工具配置${suffix}`,
      password: "12345",
      roles: [role],
    },
  });
  expect(response.ok()).toBeTruthy();
  const target: Target = {
    headers,
    role,
    username,
    id: (await response.json()).data.id as string,
  };
  createdTargets.push(target);
  return target;
}

async function login(page: Page, username = "console_admin") {
  await page.goto("/console/#users");
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(
    page.getByRole("heading", { name: "用户管理", exact: true }),
  ).toBeVisible();
}

async function open(page: Page, username: string) {
  const search = page.getByRole("textbox", { name: "搜索用户" });
  if ((await search.inputValue()) !== username) {
    await Promise.all([
      page.waitForResponse((response) => {
        const url = new URL(response.url());
        return (
          url.pathname === "/api/v1/users" &&
          url.searchParams.get("q") === username
        );
      }),
      search.fill(username),
    ]);
  }
  await expect(page.locator(".table-scroll")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  const row = page.getByRole("row").filter({ hasText: username });
  await row.getByRole("button", { name: `更多操作：${username}` }).click();
  await page
    .getByRole("menuitem", { name: "Agent 工具授权", exact: true })
    .click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByLabel("搜索工具")).toBeVisible();
  return dialog;
}

test("用户可预先授权，角色权限收回与恢复时开关保持", async ({
  page,
  request,
}) => {
  const target = await createTarget(request, "roles");
  await login(page);
  let dialog = await open(page, target.username);
  const checkbox = dialog.getByRole("checkbox", {
    name: "允许 Agent 调用：查询用户列表",
    exact: true,
  });
  await expect(checkbox).not.toBeChecked();
  await expect(checkbox).toBeEnabled();
  await checkbox.check();
  await expect(
    dialog.getByText("已允许，当前缺少业务权限", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "保存授权", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  const endpoint = `/api/v1/users/${target.id}/agent-tools`;
  const saved = (
    await (await request.get(endpoint, { headers: target.headers })).json()
  ).data;
  for (const enabled of [true, false, true]) {
    const roleResponse = await request.get(`/api/v1/roles/${target.role}`, {
      headers: target.headers,
    });
    const etag = roleResponse.headers().etag;
    if (!etag) throw new Error("角色响应缺少 ETag");
    await request.patch(`/api/v1/roles/${target.role}`, {
      headers: { ...target.headers, "If-Match": etag },
      data: {
        permissions: enabled
          ? ["user.self.read", "user.read"]
          : ["user.self.read"],
      },
    });
    dialog = await open(page, target.username);
    await expect(
      dialog.getByRole("checkbox", {
        name: "允许 Agent 调用：查询用户列表",
        exact: true,
      }),
    ).toBeChecked();
    const row = dialog
      .locator(".agent-tool-row")
      .filter({ hasText: "查询用户列表" });
    await expect(row).toContainText(
      enabled ? "业务权限满足" : "当前缺少业务权限",
    );
    const current = (
      await (await request.get(endpoint, { headers: target.headers })).json()
    ).data;
    expect(current.version).toBe(saved.version);
    await dialog.getByRole("button", { name: "取消", exact: true }).click();
  }
});

test("搜索批量授权仅影响匹配项，取消恢复，窄屏可操作", async ({
  page,
  request,
}) => {
  const target = await createTarget(request, "search");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await login(page);
  let dialog = await open(page, target.username);
  await dialog.getByLabel("搜索工具").fill("查询用户列表");
  await dialog
    .getByRole("button", { name: "允许本组匹配项", exact: true })
    .click();
  await expect(dialog.getByRole("checkbox")).toBeChecked();
  await dialog.getByLabel("搜索工具").fill("");
  await expect(
    dialog.getByRole("checkbox", {
      name: "允许 Agent 调用：删除用户",
      exact: true,
    }),
  ).not.toBeChecked();
  await page.screenshot({
    path: "test-results/agent-tools-mobile.png",
    fullPage: true,
  });
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth + 1),
  ).toBeTruthy();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  dialog = await open(page, target.username);
  await expect(
    dialog.getByRole("checkbox", {
      name: "允许 Agent 调用：查询用户列表",
      exact: true,
    }),
  ).not.toBeChecked();
  await dialog.getByLabel("搜索工具").fill("不存在的工具");
  await expect(
    dialog.getByRole("heading", { name: "没有匹配的工具" }),
  ).toBeVisible();
});

test("并发冲突保留草稿，主动载入才替换", async ({ page, request }) => {
  const target = await createTarget(request, "conflict");
  await login(page);
  const dialog = await open(page, target.username);
  const query = dialog.getByRole("checkbox", {
    name: "允许 Agent 调用：查询用户列表",
    exact: true,
  });
  await query.check();
  const endpoint = `/api/v1/users/${target.id}/agent-tools`;
  const view = (
    await (await request.get(endpoint, { headers: target.headers })).json()
  ).data;
  await request.patch(endpoint, {
    headers: target.headers,
    data: {
      expected_version: view.version,
      catalog_revision: view.catalog_revision,
      changes: [{ operation_id: "user.delete", enabled: true }],
    },
  });
  await dialog.getByRole("button", { name: "保存授权", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("载入最新信息");
  await expect(query).toBeChecked();
  await expect(
    dialog.getByRole("button", { name: "保存授权", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("button", { name: "载入最新信息（替换草稿）" })
    .click();
  await expect(query).not.toBeChecked();
  await expect(
    dialog.getByRole("checkbox", {
      name: "允许 Agent 调用：删除用户",
      exact: true,
    }),
  ).toBeChecked();
});

test("保存失败保留草稿，可重试且阻止重复提交", async ({ page, request }) => {
  const target = await createTarget(request, "failure");
  await login(page);
  const dialog = await open(page, target.username);
  const checkbox = dialog.getByRole("checkbox", {
    name: "允许 Agent 调用：查询用户列表",
    exact: true,
  });
  await checkbox.check();
  const pattern = `**/api/v1/users/${target.id}/agent-tools`;
  let writes = 0;
  await page.route(pattern, async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    writes++;
    await route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "TEMPORARY", message: "请稍后重试", details: [] },
      }),
    });
  });
  await dialog.getByRole("button", { name: "保存授权", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("请稍后重试");
  await expect(checkbox).toBeChecked();
  await page.unroute(pattern);
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(pattern, async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    writes++;
    await gate;
    await route.continue();
  });
  await dialog.getByRole("button", { name: "保存授权", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "正在保存…" }),
  ).toBeDisabled();
  await expect(checkbox).toBeDisabled();
  release();
  await expect(dialog).not.toBeVisible();
  expect(writes).toBe(2);
});

test("只读用户可查看，刷新业务权限不清空草稿", async ({ page, request }) => {
  const target = await createTarget(request, "readonly", [
    "user.self.read",
    "user.read",
  ]);
  await login(page, target.username);
  let dialog = await open(page, target.username);
  await expect(dialog.getByText(/当前为只读模式/)).toBeVisible();
  await expect(dialog.getByRole("checkbox").first()).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "保存授权", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByRole("button", { name: "退出登录", exact: true }).click();
  await login(page);
  dialog = await open(page, target.username);
  const checkbox = dialog.getByRole("checkbox", {
    name: "允许 Agent 调用：删除用户",
    exact: true,
  });
  await checkbox.check();
  await dialog.getByRole("button", { name: "刷新权限状态" }).click();
  await expect(
    dialog.getByRole("button", { name: "刷新权限状态" }),
  ).toBeEnabled();
  await expect(checkbox).toBeChecked();
  await expect(dialog.getByRole("status")).toContainText("1 项未保存");
  await page.screenshot({
    path: "test-results/agent-tools-desktop.png",
    fullPage: true,
  });
});

test("关闭加载中的弹窗不会将旧用户响应写入新用户配置", async ({
  page,
  request,
}) => {
  const first = await createTarget(request, "old");
  const second = await createTarget(request, "new");
  await login(page);
  let release: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(`**/api/v1/users/${first.id}/agent-tools`, async (route) => {
    await gate;
    await route.continue();
  });
  await page.getByRole("textbox", { name: "搜索用户" }).fill(first.username);
  await page
    .getByRole("button", { name: `更多操作：${first.username}` })
    .click();
  await page
    .getByRole("menuitem", { name: "Agent 工具授权", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "关闭弹窗" })
    .click();
  const dialog = await open(page, second.username);
  release();
  await expect(dialog.locator(".target-user")).toContainText(second.username);
  await expect(dialog.locator(".target-user")).not.toContainText(
    first.username,
  );
});
