import {
  expect,
  test,
  type APIRequestContext,
  type Page,
} from "@playwright/test";

const auditList = "/api/v1/audit/agent-operations";
const menuLabel = "Agent 操作审计";

async function administrator(request: APIRequestContext) {
  const response = await request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: "12345" },
  });
  expect(response.ok()).toBeTruthy();
  const { data } = await response.json();
  return {
    headers: { Authorization: `Bearer ${data.session_token}` },
    userId: String(data.user.id),
  };
}

async function login(
  page: Page,
  username: string,
  path = "/console/#audit",
  password = "12345",
) {
  await page.goto(path);
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录控制台" }).click();
}

test("侧栏打开审计记录并查看真实调用详情、分页和授权变更", async ({
  page,
  request,
}) => {
  const { headers, userId } = await administrator(request);
  const operationId = "user.self.read";
  const catalog = await request.get("/agent-tools", { headers });
  expect(catalog.ok()).toBeTruthy();
  const { bundleDigest } = await catalog.json();

  // Twenty-one real tool calls guarantee a second page for the active filters.
  let callId = "";
  for (let index = 0; index < 21; index += 1) {
    const response = await request.post("/agent-tools/call", {
      headers,
      data: {
        name: "agent_users_user_self_read",
        arguments: {},
        revision: bundleDigest,
      },
    });
    expect((await response.json()).ok).toBe(true);
    callId = response.headers()["x-agent-call-id"] ?? "";
    expect(callId).toMatch(/^call_/);
  }
  await expect
    .poll(async () => {
      const response = await request.get(auditList, {
        headers,
        params: {
          operation_id: operationId,
          actor_user_id: userId,
          business_outcome: "succeeded",
          limit: 20,
        },
      });
      expect(response.ok()).toBeTruthy();
      const { data } = await response.json();
      return Boolean(
        data.next_cursor &&
        data.items.some((item: { call_id: string }) => item.call_id === callId),
      );
    })
    .toBe(true);

  await login(page, "console_admin", "/console/#users");
  const menu = page.getByRole("link", { name: menuLabel });
  await menu.click();
  await expect(menu).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("heading", { name: menuLabel })).toBeVisible();
  await page.getByLabel("操作 ID", { exact: true }).fill(operationId);
  await page.getByLabel("操作者 ID", { exact: true }).fill(userId);
  await page
    .getByRole("combobox", { name: "业务结果", exact: true })
    .selectOption("succeeded");
  const filtered = page.waitForResponse((response) => {
    const url = new URL(response.url());
    return (
      url.pathname === auditList &&
      url.searchParams.get("actor_user_id") === userId &&
      url.searchParams.get("operation_id") === operationId &&
      url.searchParams.get("business_outcome") === "succeeded"
    );
  });
  await page.getByRole("button", { name: "查询", exact: true }).click();
  expect((await filtered).ok()).toBeTruthy();

  const opener = page.getByRole("button", { name: `查看详情 ${callId}` });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "操作详情" });
  await expect(dialog.getByText(callId, { exact: true })).toBeVisible();
  await expect(
    dialog.getByText("业务接口已收到", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("工具入口 · 工具授权", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("业务接口 · RBAC 权限", { exact: true }),
  ).toBeVisible();
  await dialog.getByText(/执行事件/).click();
  await expect(dialog.getByText("业务读取成功", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();

  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page.getByText("第 2 页 · 时间为北京时间")).toBeVisible();
  await expect(opener).toHaveCount(0);
  await page.getByRole("button", { name: "上一页" }).click();
  await expect(opener).toBeVisible();

  await page.getByRole("button", { name: "授权变更" }).click();
  await page.getByLabel("被授权用户 ID", { exact: true }).fill(userId);
  await page.getByLabel("操作 ID", { exact: true }).fill(operationId);
  await page.getByRole("button", { name: "查询", exact: true }).click();
  const grants = page.getByRole("table", { name: "Agent 工具授权变更" });
  await expect(
    grants.getByText("未授权 → 允许", { exact: true }),
  ).toBeVisible();
  await expect(grants.getByText(operationId, { exact: true })).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "查询", exact: true }),
  ).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(
    390,
  );
  const scroll = page.getByRole("region", { name: "授权变更列表" });
  expect(
    await scroll.evaluate(
      (element) => element.scrollWidth > element.clientWidth,
    ),
  ).toBe(true);
});

test("普通成员直接打开审计地址会跳转到个人设置", async ({ page }) => {
  const auditRequests: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname.startsWith("/api/v1/audit/"))
      auditRequests.push(request.url());
  });
  await login(
    page,
    "console_member",
    "/console/#audit",
    "Console member password 2026!",
  );
  await expect(
    page.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: menuLabel })).toHaveCount(0);
  expect(auditRequests).toEqual([]);
});

test("审计权限独立生效，恢复失败的请求并在撤权后关闭入口", async ({
  page,
  request,
}) => {
  const { headers } = await administrator(request);
  const role = "console_audit_reader";
  const username = "console_audit_reader";
  const response = await request.post("/api/v1/roles", {
    headers,
    data: {
      code: role,
      display_name: "审计查看员",
      permissions: ["user.self.read", "agent.audit.read"],
    },
  });
  expect(response.ok()).toBeTruthy();
  const etag = response.headers().etag;
  if (!etag) throw new Error("角色创建响应缺少版本");
  const user = await request.post("/api/v1/users", {
    headers,
    data: {
      username,
      display_name: "审计查看员",
      password: "12345",
      roles: [role],
    },
  });
  expect(user.ok()).toBeTruthy();

  await page.route(`**${auditList}?*`, (route) =>
    route.fulfill({
      status: 503,
      json: {
        error: {
          code: "AUDIT_UNAVAILABLE",
          message: "审计查询暂时不可用",
          details: [],
        },
      },
    }),
  );
  await login(page, username);
  await expect(page.getByRole("link", { name: menuLabel })).toHaveAttribute(
    "aria-current",
    "page",
  );
  await expect(page.getByRole("link", { name: "用户管理" })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "角色与权限" })).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText("审计查询暂时不可用");
  await page.unroute(`**${auditList}?*`);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.getByText("第 1 页 · 时间为北京时间")).toBeVisible();

  const saved = await request.patch(`/api/v1/roles/${role}`, {
    headers: { ...headers, "If-Match": etag },
    data: { permissions: ["user.self.read"] },
  });
  expect(saved.ok()).toBeTruthy();
  const denied = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === auditList &&
      response.status() === 403,
  );
  await page.getByRole("button", { name: "刷新记录" }).click();
  await denied;
  await expect(
    page.getByRole("heading", { name: "个人设置", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: menuLabel })).toHaveCount(0);
});
