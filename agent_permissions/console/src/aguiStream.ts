import { EventSchema } from "@ag-ui/core/schemas";
import type { RunAgentInput } from "@ag-ui/core";
import { ApiError } from "./errors";
import { decodeRun, type ChatRun, type RunEvent } from "./chatRuns";
import { decodeWorkflowEntry } from "./workflow";

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export async function readAguiStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  input: Pick<RunAgentInput, "threadId" | "runId">,
  onEvent: (event: RunEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let started = false;
  let closed = false;
  let sequence = 0;
  let current: ChatRun | null = null;
  const messages = new Set<string>();
  const calls = new Set<string>();
  const finishedCalls = new Set<string>();
  const steps = new Set<string>();
  const reasoning = new Set<string>();
  const reasoningMessages = new Set<string>();
  const cancel = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", cancel, { once: true });
  function emit(event: Partial<RunEvent> & Pick<RunEvent, "type">) {
    if (!current) throw new Error("AG-UI event arrived without run state");
    onEvent({ run_id: current.id, stream_id: input.runId, sequence, ...event });
  }
  function dispatch() {
    if (!data.length) return;
    if (closed) throw new Error("AG-UI data after terminal event");
    const event = EventSchema.parse(JSON.parse(data.join("\n")));
    data = [];
    sequence++;
    if (event.type === "RUN_STARTED") {
      if (
        started ||
        event.threadId !== input.threadId ||
        event.runId !== input.runId
      )
        throw new Error("AG-UI run identity changed");
      started = true;
      return;
    }
    if (!started) throw new Error("AG-UI missing RUN_STARTED");
    if (event.type === "STATE_SNAPSHOT") {
      const snapshot: unknown = event.snapshot;
      if (!object(snapshot)) throw new Error("Invalid AG-UI state");
      const run = decodeRun(snapshot.run);
      if (
        run.conversation_id !== input.threadId ||
        (current && current.id !== run.id)
      )
        throw new Error("AG-UI state belongs to another run");
      current = run;
      emit({ type: "run.snapshot", run });
    } else if (
      event.type === "ACTIVITY_SNAPSHOT" &&
      event.activityType === "workflow"
    ) {
      emit({
        type: "workflow.updated",
        entry: decodeWorkflowEntry(event.content),
      });
    } else if (event.type === "TEXT_MESSAGE_START") {
      if (messages.has(event.messageId))
        throw new Error("Repeated AG-UI message");
      messages.add(event.messageId);
    } else if (event.type === "TEXT_MESSAGE_CONTENT") {
      if (!messages.has(event.messageId))
        throw new Error("Unknown AG-UI message");
      emit({ type: "text.delta", text: event.delta });
    } else if (event.type === "TEXT_MESSAGE_END") {
      if (!messages.delete(event.messageId))
        throw new Error("Unknown AG-UI message end");
    } else if (event.type === "TOOL_CALL_START") {
      if (calls.has(event.toolCallId))
        throw new Error("Repeated AG-UI tool call");
      calls.add(event.toolCallId);
    } else if (
      event.type === "TOOL_CALL_ARGS" ||
      event.type === "TOOL_CALL_END"
    ) {
      if (!calls.has(event.toolCallId) || finishedCalls.has(event.toolCallId))
        throw new Error("Unknown AG-UI tool call");
      if (event.type === "TOOL_CALL_END") finishedCalls.add(event.toolCallId);
    } else if (event.type === "TOOL_CALL_RESULT") {
      if (!finishedCalls.has(event.toolCallId))
        throw new Error("AG-UI result before arguments completed");
    } else if (event.type === "STEP_STARTED") {
      if (steps.has(event.stepName)) throw new Error("Repeated AG-UI step");
      steps.add(event.stepName);
    } else if (event.type === "STEP_FINISHED") {
      if (!steps.delete(event.stepName)) throw new Error("Unknown AG-UI step");
    } else if (event.type === "REASONING_START") {
      if (reasoning.has(event.messageId))
        throw new Error("Repeated AG-UI reasoning");
      reasoning.add(event.messageId);
    } else if (event.type === "REASONING_MESSAGE_START") {
      if (
        !reasoning.has(event.messageId) ||
        reasoningMessages.has(event.messageId)
      )
        throw new Error("Unknown AG-UI reasoning message");
      reasoningMessages.add(event.messageId);
    } else if (event.type === "REASONING_MESSAGE_CONTENT") {
      if (!reasoningMessages.has(event.messageId))
        throw new Error("Unknown AG-UI reasoning delta");
    } else if (event.type === "REASONING_MESSAGE_END") {
      if (!reasoningMessages.delete(event.messageId))
        throw new Error("Unknown AG-UI reasoning end");
    } else if (event.type === "REASONING_END") {
      if (
        reasoningMessages.has(event.messageId) ||
        !reasoning.delete(event.messageId)
      )
        throw new Error("Invalid AG-UI reasoning lifecycle");
    } else if (event.type === "RUN_FINISHED") {
      if (
        event.threadId !== input.threadId ||
        event.runId !== input.runId ||
        !current ||
        messages.size ||
        steps.size ||
        reasoning.size ||
        reasoningMessages.size
      )
        throw new Error("AG-UI missing final state or message end");
      if (
        ["failed", "needs_reconciliation"].includes(current.status) ||
        (current.status === "running" && event.outcome?.type !== "interrupt") ||
        (current.status === "completed" &&
          event.outcome?.type === "interrupt") ||
        (current.status === "waiting" && event.outcome?.type !== "interrupt")
      )
        throw new Error("AG-UI outcome disagrees with run state");
      closed = true;
      emit({ type: "stream.closed" });
    } else if (event.type === "RUN_ERROR") {
      if (
        !current ||
        !["failed", "needs_reconciliation"].includes(current.status)
      )
        throw new ApiError(event.message, event.code || "AGUI_RUN_ERROR");
      closed = true;
      emit({ type: "stream.closed" });
    }
  }
  function lines(final = false) {
    while (true) {
      const boundary = buffer.search(/[\r\n]/);
      if (
        boundary < 0 ||
        (!final && buffer[boundary] === "\r" && boundary === buffer.length - 1)
      )
        break;
      const line = buffer.slice(0, boundary);
      const skip =
        buffer[boundary] === "\r" && buffer[boundary + 1] === "\n" ? 2 : 1;
      buffer = buffer.slice(boundary + skip);
      if (!line) dispatch();
      else if (line.startsWith("data:"))
        data.push(line.slice(5).replace(/^ /, ""));
    }
  }
  try {
    while (!closed) {
      if (signal.aborted) throw new DOMException("Stopped", "AbortError");
      const item = await reader.read();
      buffer += item.done
        ? decoder.decode()
        : decoder.decode(item.value, { stream: true });
      lines(item.done);
      if (item.done) break;
    }
    if (signal.aborted) throw new DOMException("Stopped", "AbortError");
    if (!closed)
      throw new ApiError(
        "AG-UI 连接已中断，请读取任务状态后继续",
        "CHAT_INTERRUPTED",
      );
  } catch (error) {
    if (error instanceof ApiError || error instanceof DOMException) throw error;
    throw new ApiError(
      "AG-UI 事件无法识别，请读取最新任务状态",
      "INVALID_RESPONSE",
    );
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
