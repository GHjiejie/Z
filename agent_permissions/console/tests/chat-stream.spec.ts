import { expect, test } from "@playwright/test";
import { readChatStream } from "../src/chatStream";
import { completedContext, type ChatMessage } from "../src/chatHistory";

function chunk(content: string | null = null, finish: string | null = null) {
  return JSON.stringify({
    object: "chat.completion.chunk",
    choices: [
      {
        index: 0,
        delta: content === null ? {} : { content },
        finish_reason: finish,
      },
    ],
  });
}
function stream(text: string, split = 0) {
  const encoded = new TextEncoder().encode(text);
  let offset = 0;
  return new ReadableStream<Uint8Array>({
    pull(controller) {
      if (offset === encoded.length) {
        controller.close();
        return;
      }
      const length = split || encoded.length;
      controller.enqueue(encoded.slice(offset, offset + length));
      offset = Math.min(encoded.length, offset + length);
    },
  });
}

test("SSE 解码处理 UTF-8、CRLF、注释、多行 data 和用量", async () => {
  let text = "";
  const response = `: ping\r\n\r\nevent: message\r\ndata: ${chunk("中文🙂")}\r\n\r\ndata: ${chunk("第二段", "stop")}\n\ndata: {"object":"chat.completion.chunk",\ndata: "choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}\n\ndata: [DONE]\n\n`;
  const result = await readChatStream(
    stream(response, 1),
    new AbortController().signal,
    (part) => {
      text += part;
    },
  );
  expect(text).toBe("中文🙂第二段");
  expect(result.finish).toBe("stop");
  expect(result.usage?.total_tokens).toBe(3);
});

test("SSE 流内错误保留已收到的增量，断流不能标记成功", async () => {
  let text = "";
  await expect(
    readChatStream(
      stream(
        `data: ${chunk("部分内容")}\n\ndata: {"error":{"message":"模型错误","code":"model_unavailable"}}\n\n`,
      ),
      new AbortController().signal,
      (part) => {
        text += part;
      },
    ),
  ).rejects.toMatchObject({ code: "model_unavailable" });
  expect(text).toBe("部分内容");
  for (const end of [
    "",
    "data: [DONE]\n\n",
    `data: ${chunk(null, "stop")}\n\n`,
  ]) {
    await expect(
      readChatStream(
        stream(`data: ${chunk("部分内容")}\n\n${end}`),
        new AbortController().signal,
        () => {},
      ),
    ).rejects.toMatchObject({ code: "CHAT_INTERRUPTED" });
  }
});

test("取消 SSE 会结束挂起读取并释放 reader", async () => {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    cancel() {
      cancelled = true;
    },
  });
  const controller = new AbortController();
  const pending = readChatStream(body, controller.signal, () => {});
  controller.abort();
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(cancelled).toBe(true);
  expect(body.locked).toBe(false);
});

test("中断的业务请求不随下一条消息自动重放", () => {
  const messages: ChatMessage[] = [
    {
      id: "u1",
      role: "user",
      content: "已完成的查询",
      status: "complete",
      createdAt: "2026-11-16T00:00:00Z",
    },
    {
      id: "a1",
      role: "assistant",
      content: "查询结果",
      status: "complete",
      createdAt: "2026-11-16T00:00:00Z",
    },
    {
      id: "u2",
      role: "user",
      content: "不确定是否已执行的修改",
      status: "complete",
      createdAt: "2026-11-16T00:00:00Z",
    },
    {
      id: "a2",
      role: "assistant",
      content: "部分结果",
      status: "stopped",
      createdAt: "2026-11-16T00:00:00Z",
    },
  ];
  expect(completedContext(messages)).toEqual([
    { role: "user", content: "已完成的查询" },
    { role: "assistant", content: "查询结果" },
  ]);
});
