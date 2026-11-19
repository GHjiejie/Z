import { expect, test, type Page } from "@playwright/test";

async function login(page: Page) {
  await page.goto("/console/#chat");
  await page.getByLabel("用户名", { exact: true }).fill("console_admin");
  await page.getByLabel("密码", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
  await expect(
    page.getByLabel("Agent 工具状态", { exact: true }),
  ).toContainText("可用业务工具：22 个");
}

for (const viewport of [
  { width: 1512, height: 865, minimumRatio: 0.8 },
  { width: 1920, height: 1080, minimumRatio: 0.8 },
  { width: 1280, height: 720, minimumRatio: 0.75 },
  { width: 375, height: 812, minimumRatio: 0.6 },
]) {
  test(`会话占满可用窗口，${viewport.width}×${viewport.height} 下输入和阅读区可见`, async ({
    page,
  }) => {
    await page.setViewportSize(viewport);
    await login(page);
    await expect(page.locator(".topbar")).toHaveCount(0);
    await expect(page.locator(".chat-toolbar, .page-heading")).toHaveCount(0);
    await expect(
      page.getByRole("navigation", { name: "主导航" }),
    ).toBeVisible();
    const account = page.locator(".chat-sidebar-account");
    await expect(
      account.getByRole("button", { name: "打开个人设置" }),
    ).toBeVisible();
    await expect(
      account.getByRole("button", { name: "退出登录" }),
    ).toBeVisible();
    const log = await page.getByRole("log", { name: "对话消息" }).boundingBox();
    const composer = await page.locator(".chat-composer").boundingBox();
    const thread = await page.locator(".chat-thread").boundingBox();
    const layout = await page.locator(".chat-layout").boundingBox();
    const sidebar = await page.locator(".sidebar").boundingBox();
    if (!log || !composer || !thread || !layout || !sidebar)
      throw new Error("Missing chat layout");
    await test.info().attach("viewport-measurements", {
      body: JSON.stringify({ viewport, log, composer, thread }),
      contentType: "application/json",
    });
    expect(log.height).toBeGreaterThan(viewport.height * viewport.minimumRatio);
    expect(composer.y + composer.height).toBeLessThanOrEqual(viewport.height);
    expect(thread.y + thread.height).toBe(viewport.height);
    expect(thread.x + thread.width).toBe(viewport.width);
    expect(layout.x).toBe(viewport.width > 760 ? sidebar.width : 0);
    expect(layout.y).toBe(viewport.width > 760 ? 0 : sidebar.height);
    expect(
      await page.evaluate(() => document.documentElement.scrollHeight),
    ).toBeLessThanOrEqual(viewport.height + 1);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(viewport.width);
    await page.screenshot({
      path: `test-results/chat-viewport-${viewport.width}.png`,
      fullPage: true,
    });
  });
}

test("桌面可收起会话记录增加宽度，Escape 关闭并恢复焦点", async ({ page }) => {
  await login(page);
  const panel = page.getByRole("complementary", { name: "会话记录" });
  const toggle = page.getByRole("button", { name: "会话记录", exact: true });
  await expect(panel).toBeVisible();
  const initial = await page.locator(".chat-thread").boundingBox();
  await toggle.click();
  await expect(panel).toBeHidden();
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  const expanded = await page.locator(".chat-thread").boundingBox();
  if (!initial || !expanded) throw new Error("Missing chat thread");
  expect(expanded.width - initial.width).toBeGreaterThan(200);
  await toggle.click();
  await expect(panel).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(panel).toBeHidden();
  await expect(toggle).toBeFocused();
});

test("窄屏会话记录浮层不挤压消息区域，点击内容关闭", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await login(page);
  const log = page.getByRole("log", { name: "对话消息" });
  const before = await log.boundingBox();
  await page.getByRole("button", { name: "会话记录", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "会话记录" }),
  ).toBeVisible();
  const after = await log.boundingBox();
  expect(after?.height).toBe(before?.height);
  await page.getByRole("button", { name: "收起会话记录", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "会话记录" }),
  ).toBeHidden();
  await page.getByRole("button", { name: "会话记录", exact: true }).click();
  await page.getByLabel("输入消息", { exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "会话记录" }),
  ).toBeHidden();
});

test("输入框随多行内容增长并在清空后收起，发送按钮保持可见", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await login(page);
  const input = page.getByLabel("输入消息", { exact: true });
  const initial = await input.boundingBox();
  await input.fill(
    Array.from({ length: 12 }, (_, index) => `第${index + 1}行消息`).join("\n"),
  );
  const multiline = await input.boundingBox();
  if (!initial || !multiline) throw new Error("Missing composer");
  expect(multiline.height).toBeGreaterThan(initial.height);
  expect(multiline.height).toBeLessThanOrEqual(144);
  const send = await page
    .getByRole("button", { name: "发送消息", exact: true })
    .boundingBox();
  expect(send ? send.y + send.height : Infinity).toBeLessThanOrEqual(720);
  await input.fill("参数测试".repeat(60));
  const wrapped = await input.boundingBox();
  if (!wrapped) throw new Error("Missing wrapped composer");
  await page.getByRole("button", { name: "会话记录", exact: true }).click();
  await expect
    .poll(async () => (await input.boundingBox())?.height ?? Infinity)
    .toBeLessThan(wrapped.height);
  await input.fill("");
  await expect
    .poll(async () => (await input.boundingBox())?.height)
    .toBe(initial.height);
});

test("小窗口中的 HITL 表单在消息区滚动，确认按钮可以操作", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await login(page);
  await page.getByLabel("输入消息", { exact: true }).fill("HITL 创建用户");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const form = page.locator(".tool-interaction-card");
  await expect(form).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  const confirm = form.getByRole("button", { name: "确认执行", exact: true });
  await confirm.scrollIntoViewIfNeeded();
  const box = await confirm.boundingBox();
  expect(box ? box.y + box.height : Infinity).toBeLessThanOrEqual(720);
  expect(
    await page.evaluate(() => document.documentElement.scrollHeight),
  ).toBeLessThanOrEqual(721);
  await page.screenshot({
    path: "test-results/chat-viewport-hitl.png",
    fullPage: true,
  });
  await form.getByRole("button", { name: "取消操作", exact: true }).click();
  await expect(form).toHaveCount(0);
});
