import { test, expect } from "@playwright/test";
import type { Page, TestInfo } from "@playwright/test";

// Run only against the disposable, migrated database documented in the report.
// Every data response comes from the real API. No route interception or mocks.
test.skip(
  process.env.PLATFORM_AGENT_ACCEPTANCE !== "1",
  "Requires isolated Agent acceptance API fixtures",
);
test.describe.configure({ mode: "serial" });
const password = "Agent-ui-acceptance-2026";
const createdName = "视觉验收研究助手";
let createdId = "";
let modelId = "";

async function login(
  page: Page,
  email = "admin@example.test",
  route = "#agents",
) {
  await page.goto(`/${route}`);
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "进入工作空间" }).click();
  await expect(page.getByRole("navigation", { name: "主导航" })).toBeVisible();
}
async function screenshot(page: Page, info: TestInfo, name: string) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await Promise.all(
      Array.from(document.images).map((image) => image.decode()),
    );
  });
  await page.screenshot({
    path: info.outputPath(`${name}.png`),
    animations: "disabled",
    caret: "hide",
  });
}
async function request(
  page: Page,
  path: string,
  method = "GET",
  body?: unknown,
) {
  return page.evaluate(
    async ({ path, method, body }) => {
      const scope = sessionStorage.getItem("agent-platform.tenant");
      const identity = await fetch("/api/v2/me").then((response) =>
        response.json(),
      );
      const response = await fetch(`/api/v2/tenants/${scope}${path}`, {
        method,
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": identity.csrf_token,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      return { status: response.status, body: await response.json() };
    },
    { path, method, body },
  );
}

test("真实列表、搜索、来源筛选、详情与 Logo 展开逻辑", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await login(page);
  await expect(page.locator(".agent-table tbody tr")).toHaveCount(3);
  expect(
    (await page.context().cookies()).some((cookie) => cookie.httpOnly),
  ).toBe(true);
  await page.getByLabel("在已加载的 Agent 中搜索").fill("agent_calculator");
  await expect(page.locator(".agent-table tbody tr")).toHaveCount(1);
  await page.getByLabel("在已加载的 Agent 中搜索").fill("");
  await page.locator(".agent-list-options summary").click();
  await page.getByLabel("筛选 Agent 来源").selectOption("builtin");
  await expect(
    page.getByText("未找到匹配的 Agent", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("筛选 Agent 来源").selectOption("all");
  await page.locator(".agent-list-options summary").click();
  await page
    .locator(".agent-table tbody tr")
    .filter({ hasText: "agent_calculator" })
    .getByRole("link", { name: "查看详情" })
    .click();
  await expect(page.locator(".agent-basic")).toContainText(
    "数学运算与时间查询",
  );
  await expect(page.locator(".agent-execution")).toContainText("4096");
  await expect(page).toHaveURL(/tenant=/);
  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(page.locator(".sidebar")).toHaveCSS("width", "64px");
  await expect(page.locator(".agent-collapse")).toHaveCount(0);
  const logo = page.getByRole("button", { name: "展开侧栏", exact: true });
  await expect(logo).toHaveText("Z");
  await expect(logo).not.toHaveAttribute("title");
  await logo.hover();
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await screenshot(page, info, "collapsed-detail");
  await logo.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator(".sidebar")).toHaveCSS("width", "240px");
  await page.getByRole("button", { name: "返回 Agent 列表" }).click();
  await page.getByRole("button", { name: "+ 创建 Agent", exact: true }).click();
  await expect(page.getByTestId("agent-create")).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByTestId("agent-list")).toBeVisible();
  expect(errors).toEqual([]);
});

test("字段校验、真实创建和草稿保存、发布确认/取消/成功及不可变发布", async ({
  page,
}, info) => {
  await login(page, undefined, "#agents?agent=new");
  await expect(page.getByTestId("agent-create")).toBeVisible();
  let posts = 0;
  page.on("request", (req) => {
    if (
      req.method() === "POST" &&
      /\/agents$/.test(new URL(req.url()).pathname)
    )
      posts++;
  });
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator('[name="name"]')).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await expect(page.getByRole("alert")).toContainText("请检查标记的字段");
  expect(posts).toBe(0);
  await screenshot(page, info, "field-validation");
  await page.getByLabel("Agent 名称", { exact: true }).fill(createdName);
  await page.getByLabel("说明描述", { exact: true }).fill("已发布的说明");
  await page
    .getByLabel("系统提示词 (Prompt)", { exact: true })
    .fill("保持回答严谨，提供结构化输出。");
  const models = await request(page, "/models");
  modelId = models.body.items[0].id;
  await page.getByLabel("模型偏好", { exact: true }).selectOption(modelId);
  await page.getByLabel("最大输出 Token", { exact: true }).fill("9000");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator('[name="max_tokens"]')).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  expect(posts).toBe(0);
  await page.getByLabel("最大输出 Token", { exact: true }).fill("4096");
  await page.getByLabel("Temperature", { exact: true }).fill("0.7");
  await page.getByLabel("最大步骤", { exact: true }).fill("8");
  await page.getByRole("checkbox", { name: "calculator", exact: true }).check();
  const saved = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/agents$/.test(new URL(response.url()).pathname),
  );
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  const persisted = await saved;
  expect(persisted.status()).toBe(201);
  const agent = await persisted.json();
  createdId = agent.id;
  expect(agent.published_version).toBe(0);
  expect(agent.tools).toEqual(["calculator"]);
  await expect(page.getByTestId("agent-detail")).toContainText(createdName);
  await expect(page.getByRole("status")).toContainText("配置已保存为草稿");
  await screenshot(page, info, "saved-draft");
  await page.getByRole("button", { name: "发布 Agent", exact: true }).click();
  await expect(
    page.getByRole("dialog", { name: "确认发布 Agent" }),
  ).toContainText("v1");
  await screenshot(page, info, "publish-confirmation");
  await page.getByRole("button", { name: "取消", exact: true }).click();
  expect(
    (await request(page, "/agents")).body.items.find(
      (item: { id: string }) => item.id === createdId,
    ).published_version,
  ).toBe(0);
  let publishes = 0;
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().endsWith(`/${createdId}/publish`))
      publishes++;
  });
  await page.getByRole("button", { name: "发布 Agent", exact: true }).click();
  await page.getByRole("button", { name: "确认发布", exact: true }).dblclick();
  await expect(page.getByRole("dialog", { name: "发布成功" })).toContainText(
    "v1",
  );
  expect(publishes).toBe(1);
  await screenshot(page, info, "publish-success");
  await page.getByRole("button", { name: "完成", exact: true }).click();
  await page.getByRole("button", { name: "编辑配置", exact: true }).click();
  await page
    .getByLabel("说明描述", { exact: true })
    .fill("尚未发布的私有草稿说明");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".agent-basic")).toContainText(
    "尚未发布的私有草稿说明",
  );
  expect(
    (await request(page, "/agents")).body.items.find(
      (item: { id: string }) => item.id === createdId,
    ).published_version,
  ).toBe(1);
});

test("成员只读、草稿隔离和直接访问权限检查", async ({ page }, info) => {
  await login(page, "member@example.test");
  await expect(page.locator(".agent-table")).toBeVisible();
  await expect(page.locator(".agent-table")).not.toContainText(
    "agent_draft_helper",
  );
  await expect(
    page.getByRole("button", { name: "+ 创建 Agent", exact: true }),
  ).toHaveCount(0);
  const published = (await request(page, "/agents")).body.items.find(
    (item: { id: string }) => item.id === createdId,
  );
  expect(published.description).toBe("已发布的说明");
  await page
    .locator(".agent-table tbody tr")
    .filter({ hasText: createdName })
    .getByRole("link", { name: "查看详情" })
    .click();
  await expect(page.locator(".agent-basic")).toContainText("已发布的说明");
  await expect(
    page.getByRole("button", { name: "编辑配置", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "发布新版本", exact: true }),
  ).toHaveCount(0);
  await screenshot(page, info, "member-detail");
  const denied = await request(page, "/agents", "POST", { name: "未授权创建" });
  expect(denied.status).toBe(403);
  await page.evaluate(() => {
    location.hash = "#agents?agent=new";
  });
  await expect(page.getByText("无管理权限", { exact: true })).toBeVisible();
  await screenshot(page, info, "member-create-denied");
});

test("真实模型停用发布失败，重试返回递增版本", async ({ page }, info) => {
  await login(page, undefined, `#agents?agent=${createdId}`);
  await expect(page.locator(".agent-basic")).toBeVisible();
  expect(
    (await request(page, `/models/${modelId}`, "PATCH", { active: false }))
      .status,
  ).toBe(200);
  try {
    await page.getByRole("button", { name: "发布新版本", exact: true }).click();
    await page.getByRole("button", { name: "确认发布", exact: true }).click();
    await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
      "请先启用 Agent 使用的模型",
    );
    await expect(
      page.getByRole("button", { name: "确认发布", exact: true }),
    ).toBeEnabled();
    expect(
      (await request(page, "/agents")).body.items.find(
        (item: { id: string }) => item.id === createdId,
      ).published_version,
    ).toBe(1);
    await screenshot(page, info, "publish-request-failure");
  } finally {
    expect(
      (await request(page, `/models/${modelId}`, "PATCH", { active: true }))
        .status,
    ).toBe(200);
  }
  await page.getByRole("button", { name: "确认发布", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "发布成功" })).toContainText(
    "v2",
  );
});

test("断网列表/保存错误保留输入并支持真实重试", async ({ page }, info) => {
  await login(page);
  await expect(page.locator(".agent-table")).toBeVisible();
  await page.locator(".agent-list-options summary").click();
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "刷新 Agent", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await screenshot(page, info, "list-request-failure");
  await page.context().setOffline(false);
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(page.locator(".agent-table")).toBeVisible();
  await page.getByRole("button", { name: "+ 创建 Agent", exact: true }).click();
  await page
    .getByLabel("Agent 名称", { exact: true })
    .fill("错误恢复验收 Agent");
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("暂时无法连接服务");
  await expect(page.getByLabel("Agent 名称", { exact: true })).toHaveValue(
    "错误恢复验收 Agent",
  );
  await expect(
    page.getByRole("button", { name: "保存配置", exact: true }),
  ).toBeEnabled();
  await screenshot(page, info, "save-request-failure");
  await page.context().setOffline(false);
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.locator(".agent-basic")).toContainText(
    "错误恢复验收 Agent",
  );
});

test("移动端布局、导航与非 Agent 路由外壳", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await login(page, undefined, "#agents?agent=new");
  await expect(page.getByTestId("agent-create")).toBeVisible();
  await expect(page.locator(".agent-savebar")).toHaveCSS("left", "0px");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await screenshot(page, info, "create-mobile-inferred");
  await page.getByRole("button", { name: "打开导航", exact: true }).click();
  await expect(page.locator(".sidebar.open")).toBeVisible();
  await page.getByRole("link", { name: "概览 Dashboard", exact: true }).click();
  await expect(page.locator(".agent-shell")).toHaveCount(0);
  await page.setViewportSize({ width: 1280, height: 1024 });
  await expect(page.locator(".topbar")).toHaveCSS("height", "70px");
  await expect(page.locator(".sidebar")).toHaveCSS("width", "235px");
  await screenshot(page, info, "overview-unchanged");
});
