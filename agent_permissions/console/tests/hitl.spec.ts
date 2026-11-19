import {
  expect,
  test,
  type Page,
  type APIRequestContext,
} from "@playwright/test";

async function login(page: Page, username = "console_admin") {
  await page.goto("/console/#chat");
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
}
async function send(page: Page, text: string) {
  await page.getByLabel("输入消息", { exact: true }).fill(text);
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".tool-interaction-card")).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
}
async function admin(request: APIRequestContext) {
  const response = await request.post("/api/v1/auth/login", {
    data: { username: "console_admin", password: "12345" },
  });
  return {
    Authorization: `Bearer ${(await response.json()).data.session_token}`,
  };
}
async function deleteRole(request: APIRequestContext, code: string) {
  const headers = await admin(request);
  const role = await request.get(`/api/v1/roles/${code}`, { headers });
  if (role.ok())
    await request.delete(`/api/v1/roles/${code}`, {
      headers: { ...headers, "If-Match": role.headers().etag ?? "" },
    });
}

test("写工具暂停，刷新恢复并编辑参数，重复提交只写入一次", async ({
  page,
  request,
}) => {
  const code = `hitl_${Date.now()}`;
  try {
    await login(page);
    await send(page, `HITL 创建角色 ${code}`);
    const headers = await admin(request);
    expect(
      (await request.get(`/api/v1/roles/${code}`, { headers })).status(),
    ).toBe(404);
    await expect(
      page.getByRole("button", { name: "发送消息", exact: true }),
    ).toHaveCount(0);
    await page.reload();
    const card = page.locator(".tool-interaction-card");
    await expect(card).toBeVisible();
    await card.getByLabel("展示名称", { exact: false }).fill("人工确认的角色");
    await page.screenshot({
      path: "test-results/hitl-confirm-desktop.png",
      fullPage: true,
    });
    const submitted = page.waitForRequest(
      (req) =>
        req.url().endsWith("/ag-ui") &&
        req.postDataJSON().forwardedProps?.action === "resume",
    );
    await card.getByRole("button", { name: "确认执行" }).click();
    const submission = await submitted;
    expect(
      submission
        .postDataJSON()
        .messages.some(
          (item: { role: string; content: string }) =>
            item.role === "user" && item.content.includes("HITL 创建角色"),
        ),
    ).toBe(true);
    expect(submission.postDataJSON().state.run.status).toBe("waiting");
    await expect(
      page.getByRole("article", { name: "Agent 回复" }),
    ).toContainText("操作已完成");
    const result = await request.get(`/api/v1/roles/${code}`, { headers });
    expect((await result.json()).data.display_name).toBe("人工确认的角色");
    const retry = await request.post(submission.url(), {
      headers,
      data: submission.postDataJSON(),
    });
    expect(retry.ok()).toBe(true);
    expect(await retry.text()).toContain("STATE_SNAPSHOT");
    expect(
      (await (await request.get(`/api/v1/roles/${code}`, { headers })).json())
        .data.version,
    ).toBe(1);
  } finally {
    await deleteRole(request, code);
  }
});

test("补齐创建用户参数，密码不进入记录，移动端表单可操作", async ({
  page,
  request,
}) => {
  const username = `hitl_${Date.now()}`;
  const secret = "Only-form-password-2026";
  let userId = "";
  try {
    await login(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await send(page, "HITL 创建用户");
    const card = page.locator(".tool-interaction-card");
    const optional = card.locator(".tool-optional-fields");
    await expect(optional).not.toHaveAttribute("open");
    await expect(card.getByRole("region", { name: "必填参数" })).toBeVisible();
    await card.getByLabel("用户名", { exact: false }).fill(username);
    await card.getByLabel("展示名称", { exact: false }).fill("表单用户");
    await card.getByLabel("初始密码", { exact: false }).fill(secret);
    await card.getByRole("button", { name: "显示密码", exact: true }).click();
    await expect(card.getByLabel("初始密码", { exact: false })).toHaveAttribute(
      "type",
      "text",
    );
    await card.getByRole("button", { name: "隐藏密码", exact: true }).click();
    await expect(card.getByLabel("初始密码", { exact: false })).toHaveAttribute(
      "type",
      "password",
    );
    await page.screenshot({
      path: "test-results/hitl-form-mobile-compact.png",
      fullPage: true,
    });
    await optional.locator("summary").click();
    await optional
      .getByRole("checkbox", { name: "填写邮箱", exact: true })
      .check();
    await optional.getByLabel("邮箱", { exact: true }).fill("omit@example.com");
    await optional
      .getByRole("checkbox", { name: "填写邮箱", exact: true })
      .uncheck();
    await expect(optional.getByLabel("邮箱", { exact: true })).toHaveCount(0);
    await optional
      .getByRole("checkbox", { name: "填写角色", exact: true })
      .check();
    const roles = optional.getByRole("group", { name: "角色", exact: true });
    await roles.getByRole("checkbox", { name: /（member）/ }).check();
    await roles.getByRole("checkbox", { name: /（admin）/ }).check();
    await roles.getByRole("checkbox", { name: /（admin）/ }).uncheck();
    await expect(
      roles.getByRole("checkbox", { name: /（member）/ }),
    ).toBeChecked();
    expect(
      await page.evaluate(() => JSON.stringify(localStorage)),
    ).not.toContain(secret);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: "test-results/hitl-form-mobile.png",
      fullPage: true,
    });
    const submitted = page.waitForRequest(
      (req) =>
        req.url().endsWith("/ag-ui") &&
        req.postDataJSON().forwardedProps?.action === "resume",
    );
    await card.getByRole("button", { name: "确认执行" }).click();
    const payload = (await submitted).postDataJSON().resume[0].payload;
    expect(payload.values["/body/roles"]).toEqual(["member"]);
    expect(payload.values).not.toHaveProperty("/body/email");
    expect(payload.omit).toContain("/body/email");
    expect(payload.sensitive_values["/body/password"]).toBe(secret);
    await expect(
      page.getByRole("article", { name: "Agent 回复" }),
    ).toContainText("操作已完成");
    expect(
      await page.evaluate(() => JSON.stringify(localStorage)),
    ).not.toContain(secret);
    const headers = await admin(request);
    const users = await request.get(`/api/v1/users?q=${username}`, { headers });
    userId = (await users.json()).data.items[0].id;
    await page.reload();
    await expect(page.locator(".tool-interaction-card")).toHaveCount(0);
  } finally {
    const headers = await admin(request);
    if (!userId) {
      const found = await request.get(`/api/v1/users?q=${username}`, {
        headers,
      });
      userId = (await found.json()).data.items[0]?.id ?? "";
    }
    if (userId) {
      const user = await request.get(`/api/v1/users/${userId}`, { headers });
      await request.delete(`/api/v1/users/${userId}`, {
        headers: { ...headers, "If-Match": user.headers().etag ?? "" },
      });
    }
  }
});

test("折叠中的可选字段校验失败会展开并定位，草稿可以继续修改", async ({
  page,
}) => {
  await login(page);
  await send(page, "HITL 创建用户");
  const card = page.locator(".tool-interaction-card");
  const optional = card.locator(".tool-optional-fields");
  await card
    .getByLabel("用户名", { exact: false })
    .fill("hitl_validation_only");
  await card.getByLabel("展示名称", { exact: false }).fill("参数校验草稿");
  await card.getByLabel("初始密码", { exact: false }).fill("12345");
  await optional.locator("summary").click();
  await optional
    .getByRole("checkbox", { name: "填写邮箱", exact: true })
    .check();
  const email = optional.getByLabel("邮箱", { exact: true });
  await email.fill("invalid-email");
  await optional.locator("summary").click();
  let submissions = 0;
  await page.route("**/api/v1/ag-ui", async (route) => {
    const decision = route.request().postDataJSON().resume?.[0];
    if (decision?.status === "cancelled") {
      expect(decision.payload).toMatchObject({
        values: {},
        sensitive_values: {},
        omit: [],
      });
      return route.continue();
    }
    submissions += 1;
    await route.fulfill({
      status: 422,
      json: {
        error: {
          code: "VALIDATION_ERROR",
          message: "请检查工具参数",
          details: [
            { field: "/body/email", message: "此邮箱不可用，请更换邮箱" },
          ],
        },
      },
    });
  });
  await card.getByRole("button", { name: "确认执行", exact: true }).click();
  await expect(optional).toHaveAttribute("open");
  await expect(email).toBeFocused();
  expect(submissions).toBe(0);
  await email.fill("validation@example.com");
  await optional.locator("summary").click();
  await card.getByRole("button", { name: "确认执行", exact: true }).click();
  await expect(email).toBeVisible();
  await expect(email).toHaveAttribute("aria-invalid", "true");
  await expect(email).toBeFocused();
  await expect(email).toHaveValue("validation@example.com");
  await expect(optional.getByRole("alert")).toContainText("此邮箱不可用");
  expect(submissions).toBe(1);
  await page.screenshot({
    path: "test-results/hitl-field-validation.png",
    fullPage: true,
  });
  await card.getByRole("button", { name: "取消操作", exact: true }).click();
  await expect(card).toHaveCount(0);
});

test("角色选项读取失败可重试，折叠可选参数会保留已勾选的草稿", async ({
  page,
}) => {
  let failOptions = true;
  await page.route("**/interactions/*/options?**", async (route) => {
    if (
      failOptions &&
      new URL(route.request().url()).searchParams.get("field") === "/body/roles"
    ) {
      await route.fulfill({
        status: 500,
        json: {
          error: { code: "OPTION_LOAD_FAILED", message: "角色选项读取失败" },
        },
      });
    } else {
      await route.continue();
    }
  });
  await login(page);
  await send(page, "HITL 创建用户");
  const card = page.locator(".tool-interaction-card");
  const optional = card.locator(".tool-optional-fields");
  await optional.locator("summary").click();
  await optional
    .getByRole("checkbox", { name: "填写角色", exact: true })
    .check();
  await expect(optional.getByRole("alert")).toContainText("角色选项读取失败");
  failOptions = false;
  await optional.getByRole("button", { name: "重试选项", exact: true }).click();
  const member = optional
    .getByRole("group", { name: "角色", exact: true })
    .getByRole("checkbox", { name: /（member）/ });
  await member.check();
  await optional.locator("summary").click();
  await expect(optional).not.toHaveAttribute("open");
  await optional.locator("summary").click();
  await expect(member).toBeChecked();
  await expect(optional.locator("summary")).toContainText("已填写 1 项");
  await card.getByRole("button", { name: "取消操作", exact: true }).click();
  await expect(card).toHaveCount(0);
});

test("缺少查询目标时提供真实用户选项，提交后继续回复", async ({ page }) => {
  await login(page);
  await send(page, "HITL 查询用户");
  const card = page.locator(".tool-interaction-card");
  await card.getByLabel("搜索目标用户").fill("console_member");
  const select = card.getByRole("combobox");
  await expect(select).toBeEnabled();
  await expect(
    select.getByRole("option", { name: /console_member/ }),
  ).toHaveCount(1);
  await select.selectOption(
    (await select
      .getByRole("option", { name: /console_member/ })
      .getAttribute("value"))!,
  );
  await card.getByRole("button", { name: "提交并查询" }).click();
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "操作已完成",
  );
});

test("导入 SDK 任务后旧卡片不能取消新卡片，读取最新状态后可以取消", async ({
  page,
  request,
}) => {
  await login(page);
  const code = `hitl_${Date.now()}`;
  const headers = await admin(request);
  const response = await request.post("/v1/chat/completions", {
    headers,
    data: {
      model: "fixture-model",
      messages: [{ role: "user", content: `HITL 创建角色 ${code}` }],
    },
  });
  const content = (await response.json()).choices[0].message.content;
  const id = content.match(/run_[a-f0-9]+/)?.[0];
  expect(id).toBeTruthy();
  await page.getByText("打开已有任务", { exact: true }).click();
  await page.getByLabel("任务标识").fill(id);
  await page.getByRole("button", { name: "打开任务", exact: true }).click();
  const card = page.locator(".tool-interaction-card");
  await expect(card).toBeVisible();
  const runUrl = `/api/v1/chat/runs/${id}`;
  const original = (await (await request.get(runUrl, { headers })).json()).data;
  const refresh = await request.post(
    `${runUrl}/interactions/${original.interaction.id}/refresh`,
    {
      headers,
      data: {
        request_id: "another-tab",
        expected_version: original.interaction.version,
      },
    },
  );
  expect(refresh.ok()).toBe(true);
  const replacement = (await refresh.json()).data.interaction;
  expect(replacement.id).not.toBe(original.interaction.id);
  const cancelResponse = () =>
    page.waitForResponse(
      (res) =>
        res.url().endsWith("/ag-ui") &&
        res.request().postDataJSON()?.resume?.[0]?.status === "cancelled",
    );
  const stale = cancelResponse();
  await card.getByRole("button", { name: "取消操作" }).click();
  const denied = await stale;
  expect(denied.status()).toBe(409);
  expect(denied.request().postDataJSON().resume[0]).toMatchObject({
    interruptId: original.interaction.id,
    payload: { expected_version: original.interaction.version },
  });
  await expect(card.getByRole("button", { name: "取消操作" })).toBeDisabled();
  await card.getByRole("button", { name: "读取最新任务状态" }).click();
  await expect(card.getByRole("button", { name: "取消操作" })).toBeEnabled();
  const accepted = cancelResponse();
  await card.getByRole("button", { name: "取消操作" }).click();
  const responseToCancel = await accepted;
  expect(responseToCancel.ok()).toBe(true);
  expect(responseToCancel.request().postDataJSON().resume[0]).toMatchObject({
    interruptId: replacement.id,
    status: "cancelled",
    payload: {
      decision: "cancel",
      expected_version: replacement.version,
      values: {},
      sensitive_values: {},
      omit: [],
    },
  });
  await expect(card).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "已取消",
  );
  expect(
    (await request.get(`/api/v1/roles/${code}`, { headers })).status(),
  ).toBe(404);
});

test("选择删除对象后再次确认；版本冲突只能重新准备", async ({
  page,
  request,
}) => {
  const code = `hitl_${Date.now()}`;
  const headers = await admin(request);
  await request.post("/api/v1/roles", {
    headers,
    data: { code, display_name: "冲突测试角色" },
  });
  try {
    await login(page);
    await send(page, "HITL 删除角色");
    const card = page.locator(".tool-interaction-card");
    await expect(card.getByRole("combobox")).toBeEnabled();
    await card.getByRole("combobox").selectOption(code);
    await card.getByRole("button", { name: "确认执行" }).click();
    await expect(card.locator(".tool-target")).toContainText("冲突测试角色");
    const before = await request.get(`/api/v1/roles/${code}`, { headers });
    await request.patch(`/api/v1/roles/${code}`, {
      headers: { ...headers, "If-Match": before.headers().etag ?? "" },
      data: { display_name: "已在页面修改" },
    });
    await card.getByRole("button", { name: "确认执行" }).click();
    await expect(card.getByRole("alert")).toContainText("变化");
    await expect(card.getByRole("button", { name: "确认执行" })).toBeDisabled();
    await page.screenshot({
      path: "test-results/hitl-conflict-desktop.png",
      fullPage: true,
    });
    await card.getByRole("button", { name: "重新准备并核对参数" }).click();
    await expect(card.locator(".tool-target")).toContainText("已在页面修改");
    await card.getByRole("button", { name: "取消操作" }).click();
    expect(
      (await request.get(`/api/v1/roles/${code}`, { headers })).status(),
    ).toBe(200);
  } finally {
    await deleteRole(request, code);
  }
});

test("工具授权统一在用户管理配置，返回会话后可以补参", async ({
  page,
  request,
}) => {
  const headers = await admin(request);
  const username = `chat_grants_${Date.now()}`;
  const created = await request.post("/api/v1/users", {
    headers,
    data: {
      username,
      display_name: "未授权的管理员",
      password: "12345",
      roles: ["admin"],
    },
  });
  expect(created.ok()).toBe(true);
  const userId = (await created.json()).data.id;
  try {
    await login(page, username);
    const status = page.getByLabel("Agent 工具状态", { exact: true });
    await expect(status).toContainText("当前没有可用的 Agent 业务工具");
    await expect(status).toContainText("参数表单和业务操作需要工具授权");
    await expect(status).toContainText("请在用户管理中");
    await expect(
      page.getByRole("button", {
        name: /配置 Agent 工具授权|查看 Agent 工具授权/,
      }),
    ).toHaveCount(0);
    await page.setViewportSize({ width: 375, height: 812 });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: "test-results/chat-no-tools-mobile.png",
      fullPage: true,
    });
    await page.getByRole("link", { name: "用户管理", exact: true }).click();
    await page.getByRole("textbox", { name: "搜索用户" }).fill(username);
    const row = page.getByRole("row").filter({ hasText: username });
    await row.getByRole("button", { name: `更多操作：${username}` }).click();
    await page
      .getByRole("menuitem", { name: "Agent 工具授权", exact: true })
      .click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("搜索工具").fill("user.create");
    await dialog
      .getByRole("checkbox", { name: "允许 Agent 调用：新增用户", exact: true })
      .check();
    await dialog.getByRole("button", { name: "保存授权", exact: true }).click();
    await expect(dialog).toHaveCount(0);
    await page.getByRole("link", { name: "会话", exact: true }).click();
    await expect(status).toContainText("可用业务工具：1 个");
    await expect(
      page.getByRole("button", {
        name: /配置 Agent 工具授权|查看 Agent 工具授权/,
      }),
    ).toHaveCount(0);
    await send(page, "HITL 创建用户");
    const card = page.locator(".tool-interaction-card");
    await expect(card.getByLabel("初始密码", { exact: false })).toBeVisible();
    await card.getByRole("button", { name: "取消操作", exact: true }).click();
    await expect(card).toHaveCount(0);
  } finally {
    const user = await request.get(`/api/v1/users/${userId}`, { headers });
    await request.delete(`/api/v1/users/${userId}`, {
      headers: { ...headers, "If-Match": user.headers().etag ?? "" },
    });
  }
});

test("工具状态查询失败可重试，普通聊天仍然可用", async ({ page }) => {
  let failed = true;
  await page.route("**/agent-tools", (route) =>
    failed
      ? route.fulfill({
          status: 503,
          contentType: "application/json",
          body: JSON.stringify({
            error: { message: "工具目录暂不可用", code: "SERVICE_UNAVAILABLE" },
          }),
        })
      : route.continue(),
  );
  await login(page);
  const status = page.getByLabel("Agent 工具状态", { exact: true });
  await expect(status.getByRole("alert")).toContainText("工具目录暂不可用");
  failed = false;
  await status.getByRole("button", { name: "重试", exact: true }).click();
  await expect(status).toContainText("可用业务工具：22 个");
});
