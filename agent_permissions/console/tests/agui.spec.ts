import { expect, test, type Page } from "@playwright/test";
import { readAguiStream } from "../src/aguiStream";
import type { RunEvent } from "../src/chatRuns";

const identity = { threadId: "thread-1", runId: "agui-1" };
const baseRun = {
  id: "business-1",
  conversation_id: identity.threadId,
  client_request_id: identity.runId,
  status: "running",
  version: 1,
  content: "",
  finish: "stop",
  usage: null,
  error: null,
  expires_at: 9999999999,
  purged: false,
  interaction: null,
  executions: [],
  workflow: [],
};
const started = { type: "RUN_STARTED", ...identity, protocolVersion: "1.0" };
const snapshot = (status: string, content = "") => ({
  type: "STATE_SNAPSHOT",
  snapshot: { run: { ...baseRun, status, content } },
});
const finished = {
  type: "RUN_FINISHED",
  ...identity,
  outcome: { type: "success" },
};
function wire(events: object[], bytes = false) {
  const encoded = new TextEncoder().encode(
    events
      .map((event) => `: heartbeat\r\ndata: ${JSON.stringify(event)}\r\n\r\n`)
      .join(""),
  );
  return new ReadableStream<Uint8Array>({
    start(controller) {
      if (bytes) {
        for (const byte of encoded) controller.enqueue(Uint8Array.of(byte));
      } else controller.enqueue(encoded);
      controller.close();
    },
  });
}

test("AG-UI 官方事件按 UTF-8 字节拆包，文本与实时思考活动分开更新", async () => {
  const entry = {
    id: "model-1",
    kind: "model",
    title: "分析请求",
    status: "running",
    detail: "正在响应",
    started_at: 1,
    updated_at: 2,
    reasoning: "正在核对当前账号。",
  };
  const received: RunEvent[] = [];
  await readAguiStream(
    wire(
      [
        started,
        snapshot("running"),
        {
          type: "ACTIVITY_SNAPSHOT",
          messageId: "activity-1",
          activityType: "workflow",
          content: entry,
          replace: true,
        },
        { type: "TEXT_MESSAGE_START", messageId: "answer", role: "assistant" },
        {
          type: "TEXT_MESSAGE_CONTENT",
          messageId: "answer",
          delta: "你好，处理完成。",
        },
        { type: "TEXT_MESSAGE_END", messageId: "answer" },
        snapshot("completed", "你好，处理完成。"),
        finished,
      ],
      true,
    ),
    new AbortController().signal,
    identity,
    (event) => received.push(event),
  );
  expect(received.find((event) => event.entry)?.entry?.reasoning).toBe(
    "正在核对当前账号。",
  );
  expect(
    received
      .filter((event) => event.text)
      .map((event) => event.text)
      .join(""),
  ).toBe("你好，处理完成。");
  expect(received.at(-1)?.type).toBe("stream.closed");
});

test("AG-UI 中断保留 waiting 快照，恢复输入由确认动作决定", async () => {
  const received: RunEvent[] = [];
  await readAguiStream(
    wire([
      started,
      snapshot("waiting"),
      {
        type: "RUN_FINISHED",
        ...identity,
        outcome: {
          type: "interrupt",
          interrupts: [{ id: "interaction-1", reason: "tool_interaction" }],
        },
      },
    ]),
    new AbortController().signal,
    identity,
    (event) => received.push(event),
  );
  expect(received.find((event) => event.run)?.run?.status).toBe("waiting");
  expect(received.at(-1)?.type).toBe("stream.closed");
});

test("AG-UI 拒绝跨会话数据、未开始的文本和提前返回的工具结果", async () => {
  for (const events of [
    [
      started,
      snapshot("running"),
      {
        type: "REASONING_MESSAGE_CONTENT",
        messageId: "unknown",
        delta: "invalid",
      },
    ],
    [
      started,
      snapshot("running"),
      { type: "STEP_FINISHED", stepName: "unknown" },
    ],
    [{ ...started, threadId: "foreign" }, snapshot("completed"), finished],
    [
      started,
      snapshot("running"),
      { type: "TEXT_MESSAGE_CONTENT", messageId: "unknown", delta: "invalid" },
    ],
    [
      started,
      snapshot("running"),
      {
        type: "TOOL_CALL_RESULT",
        messageId: "result",
        toolCallId: "unknown",
        content: "{}",
      },
    ],
    [
      started,
      {
        type: "STATE_SNAPSHOT",
        snapshot: { run: { ...baseRun, conversation_id: "foreign" } },
      },
    ],
  ])
    await expect(
      readAguiStream(
        wire(events),
        new AbortController().signal,
        identity,
        () => {},
      ),
    ).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
});

test("AG-UI 断流不会被当成完成，取消会释放挂起的读取", async () => {
  await expect(
    readAguiStream(
      wire([started, snapshot("completed")]),
      new AbortController().signal,
      identity,
      () => {},
    ),
  ).rejects.toMatchObject({ code: "CHAT_INTERRUPTED" });
  let cancelled = false;
  const controller = new AbortController();
  const task = readAguiStream(
    new ReadableStream({
      cancel() {
        cancelled = true;
      },
    }),
    controller.signal,
    identity,
    () => {},
  );
  controller.abort();
  await expect(task).rejects.toMatchObject({ name: "AbortError" });
  expect(cancelled).toBe(true);
});

async function login(page: Page) {
  await page.goto("/console/#chat");
  await page.getByLabel("用户名", { exact: true }).fill("console_admin");
  await page.getByLabel("密码", { exact: true }).fill("12345");
  await page.getByRole("button", { name: "登录控制台" }).click();
  await expect(page.getByLabel("聊天模型")).toHaveValue("fixture-model");
}

test("页面实时显示思考摘要、实际工具参数和结果，刷新后恢复完整过程", async ({
  page,
}) => {
  await login(page);
  let starts = 0;
  page.on("request", (request) => {
    if (
      request.url().endsWith("/api/v1/ag-ui") &&
      !request.postDataJSON().forwardedProps?.action
    )
      starts++;
  });
  await page.getByLabel("输入消息", { exact: true }).fill("AGUI 查看当前账号");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const workflow = page.locator(".agent-workflow");
  await expect(workflow.getByLabel("模型思考内容").first()).toContainText(
    "先核对当前账号权限",
  );
  await expect(
    page.getByRole("button", { name: "停止生成", exact: true }),
  ).toBeVisible();
  await expect(workflow.locator(".workflow-entry.is-running")).toContainText(
    "分析请求",
  );
  await expect(
    page.getByRole("button", { name: "停止生成", exact: true }),
  ).toHaveCount(0);
  await expect(workflow).toContainText("agent_users_user_self_read");
  const tool = workflow
    .locator(".workflow-entry")
    .filter({ hasText: "agent_users_user_self_read" });
  await expect(tool).toHaveClass(/is-succeeded/);
  await tool.getByText("调用参数", { exact: true }).click();
  await expect(tool.locator("pre").first()).toHaveText("{}");
  await tool.getByText("执行结果", { exact: true }).click();
  await expect(tool.locator("pre").last()).toContainText("console_admin");
  await tool.getByText("执行结果", { exact: true }).click();
  await page.screenshot({
    path: "test-results/agui-workflow-desktop.png",
    fullPage: true,
  });
  await page.reload();
  await expect(page.locator(".agent-workflow")).toContainText(
    "先核对当前账号权限",
  );
  await expect(
    page
      .locator(".workflow-entry.is-succeeded")
      .filter({ hasText: "agent_users_user_self_read" }),
  ).toHaveCount(1);
  expect(starts).toBe(1);
});

test("窄屏中的工具等待状态与 HITL 卡片一致，取消后不再显示执行中", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await login(page);
  await page.getByLabel("输入消息", { exact: true }).fill("HITL 创建用户");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const form = page.locator(".tool-interaction-card");
  await expect(form).toBeVisible();
  const waiting = page.locator(".workflow-entry.is-waiting");
  await expect(waiting).toContainText("新增用户");
  await expect(waiting).toContainText("agent_users_user_create");
  await page.locator(".agent-workflow").scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(375);
  await page.screenshot({
    path: "test-results/agui-workflow-mobile.png",
    fullPage: true,
  });
  await form.getByRole("button", { name: "取消操作", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(
    page.locator(".workflow-entry.is-running, .workflow-entry.is-waiting"),
  ).toHaveCount(0);
  await expect(page.locator(".agent-workflow")).toContainText("已取消");
});

test("AG-UI 连接中断时页面标记过程待核对，不继续显示执行中", async ({
  page,
}) => {
  await login(page);
  await page.route("**/api/v1/ag-ui", async (route) => {
    const input = route.request().postDataJSON();
    const entry = {
      id: "model-running",
      kind: "model",
      title: "分析请求",
      status: "running",
      detail: "正在响应",
      started_at: 1,
      updated_at: 2,
    };
    const run = {
      ...baseRun,
      conversation_id: input.threadId,
      client_request_id: input.runId,
      workflow: [entry],
    };
    const frames = [
      {
        type: "RUN_STARTED",
        threadId: input.threadId,
        runId: input.runId,
        protocolVersion: "1.0",
      },
      { type: "STATE_SNAPSHOT", snapshot: { run } },
    ];
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: frames
        .map((frame) => `data: ${JSON.stringify(frame)}\n\n`)
        .join(""),
    });
  });
  await page.getByLabel("输入消息", { exact: true }).fill("断流状态验证");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".workflow-entry.is-interrupted")).toContainText(
    "连接已中断，请读取最新任务状态",
  );
  await expect(page.locator(".workflow-entry.is-running")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "停止生成", exact: true }),
  ).toHaveCount(0);
});

test("停止生成会结束工作流中的活动步骤，刷新后保留中断记录", async ({
  page,
}) => {
  await login(page);
  await page.getByLabel("输入消息", { exact: true }).fill("AGUI 查看当前账号");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  await expect(page.locator(".workflow-reasoning").first()).toContainText(
    "先核对",
  );
  await page.getByRole("button", { name: "停止生成", exact: true }).click();
  await expect(
    page.locator(".workflow-entry.is-running, .workflow-entry.is-waiting"),
  ).toHaveCount(0);
  await page.reload();
  await expect(page.locator(".agent-workflow")).toContainText("已取消");
});
