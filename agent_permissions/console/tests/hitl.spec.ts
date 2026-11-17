import {
  expect,
  test,
  type Page,
  type APIRequestContext,
} from "@playwright/test";

async function login(page: Page) {
  await page.goto("/console/#chat");
  await page.getByLabel("用户名", { exact: true }).fill("console_admin");
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
    const submitted = page.waitForRequest((req) =>
      req.url().endsWith("/resume"),
    );
    await card.getByRole("button", { name: "确认执行" }).click();
    const submission = await submitted;
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
    expect(await retry.text()).toContain("run.snapshot");
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
    await card.getByLabel("用户名", { exact: false }).fill(username);
    await card.getByLabel("展示名称", { exact: false }).fill("表单用户");
    await card.getByLabel("初始密码", { exact: false }).fill(secret);
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
    await card.getByRole("button", { name: "确认执行" }).click();
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

test("取消待确认操作后刷新不会执行，SDK 任务可以导入", async ({
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
  await card.getByRole("button", { name: "取消操作" }).click();
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
