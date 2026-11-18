import { expect, test, type Page } from "@playwright/test";
import { readAguiStream } from "../src/aguiStream";
import type { RunEvent } from "../src/chatRuns";
import {
  decodeWorkflowEntry,
  replyParts,
  type WorkflowEntry,
} from "../src/workflow";

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
test("中断恢复、尚未到达的正文和旧记录都能保留原文及操作顺序", () => {
  const model: WorkflowEntry = {
    id: "model",
    kind: "model",
    title: "分析",
    status: "succeeded",
    detail: "",
    started_at: 1,
    updated_at: 1,
  };
  const read = {
    ...model,
    id: "read",
    kind: "tool" as const,
    answer_offset: 3,
  };
  const body = "先🔎查**结果**";
  const parts = (entries: WorkflowEntry[], content = body, live = false) =>
    replyParts(content, entries, live).map((p) =>
      p.kind === "text" ? p.text : p.entries.map((e) => e.id),
    );
  expect(parts([model])).toEqual([body, ["model"]]);
  expect(parts([{ ...model, answer_offset: 0 }, read], "先🔎", true)).toEqual([
    ["model"],
    "先🔎",
  ]);
  expect(parts([{ ...model, answer_offset: 0 }, read])).toEqual([
    ["model"],
    "先🔎查",
    ["read"],
    "**结果**",
  ]);
  expect(parts([read, { ...model, id: "retry", answer_offset: 0 }])).toEqual([
    ["read", "retry"],
    body,
  ]);
  expect(() => decodeWorkflowEntry({ ...model, answer_offset: -1 })).toThrow();
});
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

test("会话按当前账号列出工具，浏览目录不触发业务确认", async ({ page }) => {
  await login(page);
  await page
    .getByLabel("输入消息", { exact: true })
    .fill("当前账号有哪些工具？");
  await page.getByRole("button", { name: "发送消息", exact: true }).click();
  const reply = page.getByRole("article", { name: "Agent 回复" });
  await expect(reply).toContainText(/当前账号可用 \d+ 个业务工具/);
  await expect(reply).toContainText("退出当前登录");
  await expect(reply.getByRole("button", { name: "确认执行" })).toHaveCount(0);
});

test("正文按实际顺序穿插工具操作，流式展开详情和刷新均保留正文", async ({
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
  const reply = page.getByRole("article", { name: "Agent 回复" });
  const flow = reply.locator(".reply-flow");
  const activities = flow.locator("details.reply-activity");
  const thinking = activities.first();
  await expect(thinking.locator("summary")).toContainText("思考摘要");
  await expect(thinking).not.toHaveAttribute("open");
  await thinking.locator("summary").focus();
  await page.keyboard.press("Enter");
  await expect(thinking.getByLabel("模型思考内容")).toContainText(
    "先核对当前账号权限",
  );
  await expect(
    page.getByRole("button", { name: "停止生成", exact: true }),
  ).toBeVisible();
  await expect(thinking.getByLabel("模型思考内容")).toContainText(
    "再查询当前账号资料",
  );
  await expect(flow.locator(".reply-prose strong").first()).toHaveText(
    "当前账号",
  );
  await expect(
    page.getByRole("button", { name: "停止生成", exact: true }),
  ).toHaveCount(0);
  await expect(thinking).toHaveAttribute("open");
  await thinking.locator("summary").click();

  const bodies = flow.locator(".reply-prose");
  const tool = activities.nth(1);
  async function checkOrder() {
    await expect(flow.locator(":scope > *")).toHaveCount(4);
    await expect(flow.locator(":scope > :nth-child(2)")).toHaveText(
      "我先核对 🔎 当前账号。",
    );
    await expect(
      flow.locator(":scope > :nth-child(3) > summary"),
    ).toContainText("查看自己的资料");
    await expect(flow.locator(":scope > :nth-child(4)")).toHaveText(
      "当前登录账号是 console_admin。",
    );
    await expect(bodies.last().locator("strong")).toHaveText("console_admin");
    await expect(flow.locator("details[open]")).toHaveCount(0);
  }
  await checkOrder();
  await bodies.first().click();
  await reply.screenshot({ path: "test-results/reply-flow-desktop.png" });
  await tool.locator("summary").click();
  await expect(tool.getByLabel("调用参数")).toHaveText("{}");
  await expect(tool.getByLabel("执行结果")).toBeVisible();
  await expect(tool.getByLabel("执行结果")).toContainText("console_admin");
  await expect(tool).toContainText("agent_users_user_self_read");
  await page.screenshot({
    path: "test-results/reply-flow-details.png",
    fullPage: true,
  });
  await page.reload();
  await checkOrder();
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
  const activity = page.locator(".reply-activity");
  await expect(activity.locator("summary")).toContainText("新增用户");
  await expect(activity.locator("summary")).toContainText("等待你处理");
  await expect(activity).not.toHaveAttribute("open");
  await activity.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(375);
  await page.screenshot({
    path: "test-results/reply-flow-hitl-mobile.png",
    fullPage: true,
  });
  await form.getByRole("button", { name: "取消操作", exact: true }).click();
  await expect(form).toHaveCount(0);
  await expect(activity.locator("summary")).toContainText("已取消");
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
  const activity = page.locator(".reply-activity");
  await expect(activity.locator("summary")).toContainText("已中断");
  await activity.locator("summary").click();
  await expect(activity.getByLabel("操作详情")).toContainText(
    "连接已中断，请读取最新任务状态",
  );
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
  const reply = page.getByRole("article", { name: "Agent 回复" });
  const summaries = reply.locator(".reply-activity > summary");
  await expect(summaries.filter({ hasText: "思考摘要" }).first()).toBeVisible();
  await page.getByRole("button", { name: "停止生成", exact: true }).click();
  await expect(summaries.filter({ hasText: /已中断|已取消/ })).toBeVisible();
  await expect(summaries.filter({ hasText: "进行中" })).toHaveCount(0);
  await page.reload();
  await expect(summaries.filter({ hasText: "已取消" })).toBeVisible();
  await expect(summaries.filter({ hasText: "进行中" })).toHaveCount(0);
});
