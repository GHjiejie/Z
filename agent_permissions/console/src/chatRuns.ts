import { ApiError } from "./errors";
import type { ChatUsage } from "./chatStream";
import {
  decodeWorkflow,
  decodeWorkflowEntry,
  type WorkflowEntry,
} from "./workflow";

export type FieldValue = string | number | boolean | string[] | null;
export interface ToolField {
  pointer: string;
  label: string;
  type: "string" | "number" | "integer" | "boolean" | "array";
  required: boolean;
  nullable: boolean;
  sensitive: boolean;
  editable: boolean;
  present: boolean;
  value?: FieldValue;
  source: "users" | "roles" | "permissions" | null;
  options: ToolOption[];
  constraints: Record<string, string | number>;
}
export interface ToolOption {
  value: string;
  label: string;
}
export interface ToolInteraction {
  id: string;
  version: number;
  status: string;
  expires_at: number;
  title: string;
  kind: "collect_parameters" | "confirm_action";
  confirmation_required: boolean;
  effect: "read" | "write" | "destructive";
  description: string;
  fields: ToolField[];
  target: Record<string, unknown> | null;
}
export type RunStatus =
  | "running"
  | "waiting"
  | "completed"
  | "cancelled"
  | "expired"
  | "failed"
  | "interrupted"
  | "needs_reconciliation";
export interface ChatRun {
  workflow?: WorkflowEntry[];
  id: string;
  conversation_id: string;
  client_request_id: string;
  status: RunStatus;
  version: number;
  content: string;
  finish: "stop" | "length";
  usage: ChatUsage | null;
  error: { code: string; message: string } | null;
  expires_at: number;
  purged: boolean;
  interaction: ToolInteraction | null;
  executions: {
    id: string;
    operation_id: string;
    status: string;
    result: unknown;
  }[];
}
export interface ToolSubmission {
  submission_id: string;
  expected_version: number;
  decision: "submit";
  values: Record<string, FieldValue>;
  sensitive_values: Record<string, FieldValue>;
  omit: string[];
}
export interface RunEvent {
  entry?: WorkflowEntry;
  type: string;
  run_id: string;
  stream_id: string;
  sequence: number;
  run?: ChatRun;
  text?: string;
}

function object(value: unknown): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value))
    throw new Error("Expected object");
  return value as Record<string, unknown>;
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Expected string");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("Expected number");
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Expected boolean");
  return value;
}
function choice<T extends string>(value: unknown, choices: readonly T[]): T {
  const match = choices.find((item) => item === value);
  if (match === undefined) throw new Error("Invalid enum");
  return match;
}
function list<T>(value: unknown, decode: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new Error("Expected array");
  return value.map(decode);
}
export function decodeOptions(value: unknown): ToolOption[] {
  return list(value, (item) => {
    const row = object(item);
    return { value: text(row.value), label: text(row.label) };
  });
}
function fieldValue(value: unknown): FieldValue {
  if (value === null || typeof value === "string" || typeof value === "boolean")
    return value;
  if (typeof value === "number") return number(value);
  return list(value, text);
}
function interaction(value: unknown): ToolInteraction {
  const item = object(value);
  return {
    id: text(item.id),
    version: number(item.version),
    status: text(item.status),
    expires_at: number(item.expires_at),
    title: text(item.title),
    description: text(item.description),
    confirmation_required: boolean(item.confirmation_required),
    kind: choice(item.kind, ["collect_parameters", "confirm_action"]),
    effect: choice(item.effect, ["read", "write", "destructive"]),
    target: item.target === null ? null : object(item.target),
    fields: list<ToolField>(item.fields, (value) => {
      const field = object(value);
      const constraints: Record<string, string | number> = {};
      for (const [key, val] of Object.entries(object(field.constraints))) {
        if (typeof val !== "string" && typeof val !== "number")
          throw new Error("Invalid constraint");
        constraints[key] = val;
      }
      return {
        pointer: text(field.pointer),
        label: text(field.label),
        type: choice(field.type, [
          "string",
          "number",
          "integer",
          "boolean",
          "array",
        ]),
        required: boolean(field.required),
        nullable: boolean(field.nullable),
        sensitive: boolean(field.sensitive),
        editable: boolean(field.editable),
        present: boolean(field.present),
        ...(field.value === undefined
          ? {}
          : { value: fieldValue(field.value) }),
        source:
          field.source === null
            ? null
            : choice<NonNullable<ToolField["source"]>>(field.source, [
                "users",
                "roles",
                "permissions",
              ]),
        options: decodeOptions(field.options),
        constraints,
      };
    }),
  };
}
export function decodeRun(value: unknown): ChatRun {
  const item = object(value);
  const usage = item.usage === null ? null : object(item.usage);
  const error = item.error === null ? null : object(item.error);
  return {
    id: text(item.id),
    conversation_id: text(item.conversation_id),
    client_request_id: text(item.client_request_id),
    status: choice(item.status, [
      "running",
      "waiting",
      "completed",
      "cancelled",
      "expired",
      "failed",
      "interrupted",
      "needs_reconciliation",
    ]),
    version: number(item.version),
    content: text(item.content),
    finish: choice(item.finish, ["stop", "length"]),
    expires_at: number(item.expires_at),
    purged: boolean(item.purged),
    workflow: item.workflow === undefined ? [] : decodeWorkflow(item.workflow),
    usage: usage
      ? {
          prompt_tokens: number(usage.prompt_tokens),
          completion_tokens: number(usage.completion_tokens),
          total_tokens: number(usage.total_tokens),
        }
      : null,
    error: error
      ? { code: text(error.code), message: text(error.message) }
      : null,
    interaction:
      item.interaction === null ? null : interaction(item.interaction),
    executions: list(item.executions, (value) => {
      const row = object(value);
      return {
        id: text(row.id),
        operation_id: text(row.operation_id),
        status: text(row.status),
        result: row.result,
      };
    }),
  };
}
export function decodeRuns(value: unknown): ChatRun[] {
  return list(value, decodeRun);
}

export async function readRunStream(
  body: ReadableStream<Uint8Array>,
  signal: AbortSignal,
  onEvent: (event: RunEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let data: string[] = [];
  let eventName = "";
  let closed = false;
  let streamId = "";
  let sequence = 0;
  let runId = "";
  let finalStatus: string | null = null;
  const cancel = () => {
    void reader.cancel().catch(() => {});
  };
  signal.addEventListener("abort", cancel, { once: true });
  function dispatch() {
    if (!data.length) return;
    const item = object(JSON.parse(data.join("\n")));
    const type = text(item.type);
    if (
      type !== eventName ||
      item.protocol_version !== 1 ||
      number(item.sequence) !== sequence + 1
    )
      throw new Error("Invalid event ordering");
    const incoming = text(item.stream_id);
    if (streamId && incoming !== streamId) throw new Error("Stream changed");
    streamId = incoming;
    sequence++;
    const event: RunEvent = {
      type,
      run_id: text(item.run_id),
      stream_id: streamId,
      sequence,
    };
    if (runId && runId !== event.run_id) throw new Error("Run changed");
    runId = event.run_id;
    if (item.run !== undefined) {
      event.run = decodeRun(item.run);
      if (event.run.id !== runId)
        throw new Error("Snapshot belongs to another run");
      if (
        [
          "run.snapshot",
          "run.waiting",
          "run.completed",
          "run.failed",
          "run.cancelled",
          "run.interrupted",
          "run.needs_reconciliation",
        ].includes(type)
      )
        finalStatus = event.run.status;
    }
    if (type === "text.delta") event.text = text(item.text);
    if (type === "workflow.updated")
      event.entry = decodeWorkflowEntry(item.entry);
    if (type === "stream.closed") {
      if (!finalStatus || finalStatus !== item.status)
        throw new ApiError(
          "任务流缺少最终状态，请读取最新任务",
          "CHAT_INTERRUPTED",
        );
      closed = true;
    }
    onEvent(event);
    data = [];
    eventName = "";
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
      else if (line.startsWith("event:")) eventName = line.slice(6).trim();
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
        "连接已中断，请读取任务状态后继续",
        "CHAT_INTERRUPTED",
      );
  } catch (error) {
    if (error instanceof ApiError || error instanceof DOMException) throw error;
    throw new ApiError("任务事件无法识别，请读取最新状态", "INVALID_RESPONSE");
  } finally {
    signal.removeEventListener("abort", cancel);
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
