import { expect, test, type Page } from "@playwright/test";

async function login(
  page: Page,
  username = "console_admin",
  password = "12345",
) {
  await page.goto("/console/#chat");
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(
    page.getByRole("region", { name: "Agent 会话", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
}
async function fixture(page: Page) {
  await page.route("**/api/v1/ag-ui", async (route) => {
    await route.continue({
      url: new URL("/__test/chat-stream", route.request().url()).href,
    });
  });
}
async function send(page: Page, message: string) {
  await page
    .getByRole("textbox", { name: "输入消息", exact: true })
    .fill(message);
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
}

test("真实 Chat 接口调用 Agent 并显示回复", async ({ page }) => {
  await login(page);
  await send(page, "查看我的账号");
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "console_admin",
  );
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  await page.screenshot({
    path: "test-results/chat-desktop.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("SSE 分段呈现，携带上下文并在刷新后恢复记录", async ({ page }) => {
  await fixture(page);
  await login(page);
  const requests: unknown[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/ag-ui"))
      requests.push(request.postDataJSON());
  });
  await send(page, "分段回复验证");
  const answer = page.getByRole("article", { name: "Agent 回复" }).first();
  await expect(answer).toContainText("第一段");
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await expect(answer).not.toContainText("第二段");
  await expect(answer).toContainText("第二段");
  await expect(answer).toContainText("30 tokens");
  await send(page, "接着聊");
  await expect(
    page.getByRole("article", { name: "Agent 回复" }).last(),
  ).toContainText("第二段");
  expect(requests[1]).toMatchObject({
    forwardedProps: { model: "fixture-model" },
    messages: [
      { role: "user", content: "分段回复验证" },
      {
        role: "assistant",
        content: "第一段：正在处理你的请求。\n第二段：回复已经完成。",
      },
      { role: "user", content: "接着聊" },
    ],
  });
  await page.getByLabel("输入消息").fill("刷新后还在的草稿");
  await expect
    .poll(() =>
      page.evaluate(() =>
        Object.keys(localStorage).some(
          (key) =>
            key.startsWith("agent-console-chat-v1:") &&
            localStorage.getItem(key)?.includes("刷新后还在的草稿"),
        ),
      ),
    )
    .toBe(true);
  await page.reload();
  await expect(page.getByLabel("输入消息")).toHaveValue("刷新后还在的草稿");
  await expect(page.getByRole("article", { name: "Agent 回复" })).toHaveCount(
    2,
  );
});

test("SSE 增量渲染 Markdown，复制原文并适配窄屏", async ({ page }) => {
  await fixture(page);
  await login(page);
  await send(page, "Markdown 渲染验证");
  const answer = page.getByRole("article", { name: "Agent 回复" });
  await expect(
    answer.getByRole("heading", { name: "流式 Markdown", level: 2 }),
  ).toBeVisible();
  await expect(answer.locator("strong", { hasText: "重点" })).toBeVisible();
  await expect(answer.locator("pre code")).toHaveText('print("hello');
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await expect(answer.locator("pre code")).toHaveText(
    'print("hello Markdown")\nprint("<safe>")\n',
  );
  await expect(answer.locator("code.language-python")).toHaveCount(1);
  await expect(answer.locator("blockquote")).toContainText("引用说明");
  await expect(answer.locator("ol > li")).toHaveText(["第一步", "第二步"]);
  await expect(answer.locator("del")).toHaveText("删除的内容");
  await expect(answer.getByRole("checkbox").first()).toBeChecked();
  await expect(answer.getByRole("checkbox").last()).not.toBeChecked();
  await expect(answer.getByRole("cell", { name: "alice" })).toBeAttached();
  const link = answer.getByRole("link", { name: "项目文档" });
  await expect(link).toHaveAttribute("href", "https://example.com/guide");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  await expect(answer.getByRole("link", { name: "危险链接" })).toHaveCount(0);
  await expect(answer.locator("script, img")).toHaveCount(0);
  expect(
    await page.evaluate(() => Object.hasOwn(window, "markdownInjected")),
  ).toBe(false);
  await expect(answer).toContainText("30 tokens");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await answer.getByRole("button", { name: "复制回复" }).click();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("## 流式 Markdown");
  expect(copied).toContain('```python\nprint("hello Markdown")');
  expect(copied).toContain("| alice | 管理员 | 启用 | 测试记录 |");
  await page.reload();
  await expect(answer.locator("pre code")).toContainText("hello Markdown");
  await page.getByRole("log").evaluate((node) => {
    node.scrollTop = 0;
  });
  await page.screenshot({
    path: "test-results/chat-markdown-desktop.png",
    fullPage: true,
    animations: "disabled",
  });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 812 });
  const table = answer.getByRole("region", { name: "Markdown 表格" });
  await table.scrollIntoViewIfNeeded();
  expect(
    await table.evaluate((node) => node.scrollWidth > node.clientWidth),
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/chat-markdown-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("未闭合的 Markdown 代码块在停止和刷新后仍能展示", async ({ page }) => {
  await fixture(page);
  await login(page);
  await send(page, "Markdown 慢流验证");
  const answer = page.getByRole("article", { name: "Agent 回复" });
  await expect(answer.locator("pre code")).toHaveText('print("hello');
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(answer).toContainText("已停止生成");
  await expect(answer.locator("pre code")).toHaveText('print("hello');
  await page.reload();
  await expect(answer.locator("pre code")).toHaveText('print("hello');
  await expect(answer).toContainText("已停止生成");
});

test("新建、切换和删除会话保留各自草稿", async ({ page }) => {
  await login(page);
  await page.getByLabel("输入消息").fill("第一份草稿");
  await page
    .getByRole("button", { name: "新建会话", exact: true })
    .last()
    .click();
  await page.getByLabel("输入消息").fill("第二份草稿");
  await page.locator(".conversation-select").nth(1).click();
  await expect(page.getByLabel("输入消息")).toHaveValue("第一份草稿");
  await page
    .locator(".conversation-row")
    .nth(1)
    .getByRole("button", { name: /删除会话/ })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "确认删除" })
    .click();
  await expect(page.locator(".conversation-row")).toHaveCount(1);
  await expect(page.getByLabel("输入消息")).toHaveValue("第二份草稿");
});

test("停止生成保留部分内容，下一次发送不重放中断任务", async ({ page }) => {
  await fixture(page);
  await login(page);
  const bodies: unknown[] = [];
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/ag-ui"))
      bodies.push(request.postDataJSON());
  });
  await send(page, "慢流操作");
  const first = page.getByRole("article", { name: "Agent 回复" }).first();
  await expect(first).toContainText("第一段");
  await page.getByRole("button", { name: "停止生成" }).click();
  await expect(first).toContainText("已停止生成");
  await expect(first).toContainText("部分业务操作可能已执行");
  await send(page, "查询确认结果");
  await expect(
    page.getByRole("article", { name: "Agent 回复" }).last(),
  ).toContainText("第二段");
  await expect(first).not.toContainText("第二段");
  expect(bodies[1]).toMatchObject({
    messages: [{ role: "user", content: "查询确认结果" }],
  });
});

test("流内错误与断流保留回复，编辑原消息不自动重试", async ({ page }) => {
  await fixture(page);
  await login(page);
  let requests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/ag-ui")) requests++;
  });
  await send(page, "错误验证");
  const first = page.getByRole("article", { name: "Agent 回复" }).first();
  await expect(first.getByRole("alert")).toContainText("模型暂时繁忙");
  await expect(first).toContainText("第一段");
  await first.getByRole("button", { name: "编辑原消息" }).click();
  await expect(page.getByLabel("输入消息")).toHaveValue("错误验证");
  expect(requests).toBe(1);
  await send(page, "断流验证");
  await expect(
    page.getByRole("article", { name: "Agent 回复" }).last().getByRole("alert"),
  ).toContainText("连接已中断");
});

test("退出中的流不会污染其他账号，会话历史按账号隔离", async ({ page }) => {
  await fixture(page);
  await login(page);
  await send(page, "管理员慢流记录");
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "第一段",
  );
  await page.getByRole("button", { name: "退出登录" }).click();
  await login(page, "console_member", "Console member password 2026!");
  await expect(page.getByText("从一个问题开始", { exact: true })).toBeVisible();
  await expect(page.getByRole("article")).toHaveCount(0);
  await expect(page.getByLabel("输入消息")).toHaveValue("");
  await page.getByRole("button", { name: "退出登录" }).click();
  await login(page);
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "已停止生成",
  );
});

test("连续生成时刷新可恢复部分内容，且不会重新发起任务", async ({ page }) => {
  await fixture(page);
  await login(page);
  let requests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/ag-ui")) requests++;
  });
  await send(page, "长回复刷新验证");
  await expect
    .poll(() =>
      page.evaluate(() =>
        Object.keys(localStorage).some(
          (key) =>
            key.startsWith("agent-console-chat-v1:") &&
            localStorage.getItem(key)?.includes("第 8 项"),
        ),
      ),
    )
    .toBe(true);
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await page.reload();
  const answer = page.getByRole("article", { name: "Agent 回复" });
  await expect(answer).toContainText("第 8 项");
  await expect(answer).toContainText("已停止生成");
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  expect(requests).toBe(1);
});

test("模型加载失败保留草稿，可主动重试恢复", async ({ page }) => {
  await login(page);
  await page.getByLabel("输入消息").fill("等待模型恢复的草稿");
  await page.route("**/v1/models", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          message: "模型服务暂不可用",
          code: "model_unavailable",
          type: "server_error",
        },
      }),
    }),
  );
  await page.getByRole("button", { name: "刷新模型列表" }).click();
  await expect(page.getByRole("alert")).toContainText("模型服务暂不可用");
  await expect(page.getByRole("button", { name: "发送消息" })).toBeDisabled();
  await page.unroute("**/v1/models");
  await page.getByRole("alert").getByRole("button", { name: "重试" }).click();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
  await expect(page.getByLabel("输入消息")).toHaveValue("等待模型恢复的草稿");
  await expect(page.getByRole("button", { name: "发送消息" })).toBeEnabled();
});

test("损坏的本地记录不会被覆盖，当前页面仍可聊天", async ({ page }) => {
  await login(page);
  const key = await page.evaluate(() => {
    const key = Object.keys(localStorage).find((item) =>
      item.startsWith("agent-console-chat-v1:"),
    );
    if (!key) throw new Error("Missing saved history");
    localStorage.setItem(key, "broken history");
    return key;
  });
  await page.reload();
  await expect(page.getByText(/本地记录无法读取/)).toBeVisible();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
  await send(page, "查看我的账号");
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "console_admin",
  );
  expect(await page.evaluate((key) => localStorage.getItem(key), key)).toBe(
    "broken history",
  );
});

test("中文输入法与换行不会误发送，401 返回登录页", async ({ page }) => {
  await fixture(page);
  await login(page);
  const input = page.getByLabel("输入消息");
  let requests = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/v1/ag-ui")) requests++;
  });
  await input.fill("输入法中的问题");
  await input.dispatchEvent("compositionstart");
  await input.press("Enter");
  expect(requests).toBe(0);
  await input.dispatchEvent("compositionend");
  await input.press("Shift+Enter");
  expect(requests).toBe(0);
  await input.press("Enter");
  await expect(page.getByRole("article", { name: "Agent 回复" })).toContainText(
    "第二段",
  );
  await page.unroute("**/api/v1/ag-ui");
  await page.route("**/api/v1/ag-ui", (route) =>
    route.fulfill({
      status: 401,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          message: "请重新登录",
          code: "UNAUTHENTICATED",
          type: "authentication_error",
        },
      }),
    }),
  );
  await send(page, "会话过期");
  await expect(page.getByRole("button", { name: "登录控制台" })).toBeVisible();
  await expect(page.getByRole("status")).toContainText("登录状态已失效");
});

test("长回复保留阅读位置，窄屏与减少动效下可操作", async ({ page }) => {
  await fixture(page);
  await login(page);
  await send(page, "长回复验证");
  const log = page.getByRole("log", { name: "对话消息" });
  await expect
    .poll(() =>
      log.evaluate((node) => node.scrollHeight > node.clientHeight + 150),
    )
    .toBe(true);
  await log.evaluate((node) => {
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await expect(
    page.getByRole("button", { name: "回到最新消息" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  expect(await log.evaluate((node) => node.scrollTop)).toBe(0);
  await page.getByRole("button", { name: "回到最新消息" }).click();
  await expect
    .poll(() =>
      log.evaluate(
        (node) => node.scrollHeight - node.clientHeight - node.scrollTop,
      ),
    )
    .toBeLessThan(64);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 375, height: 812 });
  await page.getByRole("button", { name: "会话记录", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "会话记录" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/chat-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
});
