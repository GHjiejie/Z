import { ApiError } from "./errors";

export interface ChatMessageInput {
  role: "user" | "assistant";
  content: string;
}
export interface ChatUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}
export interface ChatStreamResult {
  finish: "stop" | "length";
  usage?: ChatUsage;
}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new ApiError(
      "回复数据无法识别，请先核对操作结果",
      "INVALID_RESPONSE",
    );
  return value as Record<string, unknown>;
}

export function chatFailure(value: unknown, status = 0): ApiError {
  const payload = record(value);
  const failure = record(payload.error);
  return new ApiError(
    typeof failure.message === "string"
      ? failure.message
      : "回复未能完成，请稍后重试",
    typeof failure.code === "string" ? failure.code : "CHAT_FAILED",
    status,
  );
}

export async function readChatStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onText: (text: string) => void,
): Promise<ChatStreamResult> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let done = false;
  let finish: "stop" | "length" | undefined;
  let usage: ChatUsage | undefined;
  const abort = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", abort, { once: true });
  function checkAbort() {
    if (signal.aborted) throw new DOMException("Chat stopped", "AbortError");
  }
  function dispatch() {
    if (!data.length) return;
    const event = data.join("\n");
    data = [];
    if (event === "[DONE]") {
      done = true;
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(event);
    } catch {
      throw new ApiError(
        "回复数据不完整，请先核对操作结果",
        "INVALID_RESPONSE",
      );
    }
    const payload = record(parsed);
    if (payload.error) throw chatFailure(payload);
    if (
      payload.object !== "chat.completion.chunk" ||
      !Array.isArray(payload.choices)
    )
      throw new ApiError("回复格式不符合要求", "INVALID_RESPONSE");
    for (const value of payload.choices) {
      const choice = record(value);
      if (choice.index !== 0)
        throw new ApiError("回复格式不符合要求", "INVALID_RESPONSE");
      const delta = record(choice.delta);
      if (delta.content !== undefined && delta.content !== null) {
        if (typeof delta.content !== "string")
          throw new ApiError("回复格式不符合要求", "INVALID_RESPONSE");
        if (finish)
          throw new ApiError("回复状态不符合要求", "INVALID_RESPONSE");
        onText(delta.content);
      }
      if (choice.finish_reason !== undefined && choice.finish_reason !== null) {
        if (
          choice.finish_reason !== "stop" &&
          choice.finish_reason !== "length"
        )
          throw new ApiError(
            "回复未能完整生成，请先核对操作结果",
            "CHAT_INCOMPLETE",
          );
        finish = choice.finish_reason;
      }
    }
    if (payload.usage !== undefined && payload.usage !== null) {
      const item = record(payload.usage);
      const prompt = item.prompt_tokens;
      const completion = item.completion_tokens;
      const total = item.total_tokens;
      if (
        typeof prompt !== "number" ||
        typeof completion !== "number" ||
        typeof total !== "number" ||
        ![prompt, completion, total].every(
          (n) => Number.isSafeInteger(n) && n >= 0,
        )
      )
        throw new ApiError("回复用量无法识别", "INVALID_RESPONSE");
      usage = {
        prompt_tokens: prompt,
        completion_tokens: completion,
        total_tokens: total,
      };
    }
  }
  function lines(final: boolean) {
    while (!done) {
      const boundary = buffer.search(/[\r\n]/);
      if (
        boundary < 0 ||
        (!final && buffer[boundary] === "\r" && boundary === buffer.length - 1)
      )
        break;
      const line = buffer.slice(0, boundary);
      const size =
        buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
      buffer = buffer.slice(boundary + size);
      if (line === "") dispatch();
      else if (line.startsWith("data:"))
        data.push(line.slice(5).replace(/^ /, ""));
    }
  }
  try {
    checkAbort();
    while (!done) {
      checkAbort();
      const next = await reader.read();
      checkAbort();
      buffer += next.done
        ? decoder.decode()
        : decoder.decode(next.value, { stream: true });
      lines(next.done);
      if (next.done) break;
    }
    if (!done || !finish)
      throw new ApiError(
        "连接已中断，回复可能不完整，请先核对操作结果",
        "CHAT_INTERRUPTED",
      );
    return { finish, usage };
  } catch (error) {
    checkAbort();
    if (error instanceof ApiError) throw error;
    throw new ApiError(
      "连接已中断，回复可能不完整，请先核对操作结果",
      "CHAT_INTERRUPTED",
    );
  } finally {
    signal.removeEventListener("abort", abort);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
