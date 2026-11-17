import { expect, test } from "@playwright/test";
import { readRunStream, type RunEvent } from "../src/chatRuns";
const run = {
  id: "run-test",
  conversation_id: "conversation",
  client_request_id: "request",
  status: "waiting",
  version: 2,
  content: "中文🙂",
  finish: "stop",
  usage: null,
  error: null,
  expires_at: 1,
  purged: false,
  interaction: null,
  executions: [],
};
function event(type: string, sequence: number, extra: object = {}) {
  return `event: ${type}\r\ndata: ${JSON.stringify({ type, sequence, protocol_version: 1, run_id: run.id, stream_id: "segment", ...extra })}\r\n\r\n`;
}
function body(input: string) {
  const bytes = new TextEncoder().encode(input);
  let offset = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset === bytes.length) controller.close();
      else controller.enqueue(bytes.slice(offset, ++offset));
    },
  });
}
test("交互 SSE 在逐字节拆包后保留中文、事件顺序和等待状态", async () => {
  const received: RunEvent[] = [];
  await readRunStream(
    body(
      ": ping\r\n\r\n" +
        event("text.delta", 1, { text: "中文🙂" }) +
        event("run.waiting", 2, { run }) +
        event("stream.closed", 3, { status: "waiting" }),
    ),
    new AbortController().signal,
    (value) => received.push(value),
  );
  expect(received.map((value) => value.type)).toEqual([
    "text.delta",
    "run.waiting",
    "stream.closed",
  ]);
  expect(received[0]?.text).toBe("中文🙂");
  expect(received[1]?.run?.status).toBe("waiting");
});
test("流结束不能代替任务状态，断流与跨任务快照会被拒绝", async () => {
  for (const input of [
    event("text.delta", 1, { text: "partial" }),
    event("stream.closed", 1, { status: "completed" }),
    event("run.waiting", 1, { run: { ...run, id: "other" } }),
    event("run.waiting", 2, { run }),
  ]) {
    await expect(
      readRunStream(body(input), new AbortController().signal, () => {}),
    ).rejects.toBeDefined();
  }
});
test("重复请求可返回 running 快照并关闭，客户端不误标为完成", async () => {
  const received: RunEvent[] = [];
  await readRunStream(
    body(
      event("run.snapshot", 1, { run: { ...run, status: "running" } }) +
        event("stream.closed", 2, { status: "running" }),
    ),
    new AbortController().signal,
    (value) => received.push(value),
  );
  expect(received[0]?.run?.status).toBe("running");
});
test("取消交互 SSE 释放正在等待的读取", async () => {
  let cancelled = false;
  const stream = new ReadableStream<Uint8Array>({
    cancel() {
      cancelled = true;
    },
  });
  const controller = new AbortController();
  const pending = readRunStream(stream, controller.signal, () => {});
  controller.abort();
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(cancelled).toBe(true);
  expect(stream.locked).toBe(false);
});
