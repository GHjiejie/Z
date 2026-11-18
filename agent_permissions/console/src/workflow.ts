export interface WorkflowEntry {
  id: string;
  kind: "step" | "model" | "tool";
  title: string;
  status:
    | "pending"
    | "running"
    | "waiting"
    | "succeeded"
    | "failed"
    | "cancelled"
    | "interrupted";
  detail: string;
  started_at: number;
  updated_at: number;
  answer_offset?: number;
  reasoning?: string;
  tool_name?: string;
  operation_id?: string;
  arguments?: unknown;
  result?: unknown;
}

export function decodeWorkflowEntry(value: unknown): WorkflowEntry {
  if (typeof value !== "object" || value === null || Array.isArray(value))
    throw new Error("Invalid workflow entry");
  const row = value as Record<string, unknown>;
  if (
    typeof row.id !== "string" ||
    typeof row.title !== "string" ||
    typeof row.detail !== "string" ||
    !["step", "model", "tool"].includes(String(row.kind)) ||
    ![
      "pending",
      "running",
      "waiting",
      "succeeded",
      "failed",
      "cancelled",
      "interrupted",
    ].includes(String(row.status)) ||
    typeof row.started_at !== "number" ||
    !Number.isFinite(row.started_at) ||
    typeof row.updated_at !== "number" ||
    !Number.isFinite(row.updated_at) ||
    (row.answer_offset !== undefined &&
      (typeof row.answer_offset !== "number" ||
        !Number.isSafeInteger(row.answer_offset) ||
        row.answer_offset < 0)) ||
    (row.reasoning !== undefined && typeof row.reasoning !== "string") ||
    (row.tool_name !== undefined && typeof row.tool_name !== "string") ||
    (row.operation_id !== undefined && typeof row.operation_id !== "string")
  )
    throw new Error("Invalid workflow fields");
  return {
    id: row.id,
    title: row.title,
    detail: row.detail,
    kind: row.kind as WorkflowEntry["kind"],
    status: row.status as WorkflowEntry["status"],
    started_at: row.started_at,
    updated_at: row.updated_at,
    ...(typeof row.answer_offset === "number"
      ? { answer_offset: row.answer_offset }
      : {}),
    ...(typeof row.reasoning === "string" ? { reasoning: row.reasoning } : {}),
    ...(typeof row.tool_name === "string" ? { tool_name: row.tool_name } : {}),
    ...(typeof row.operation_id === "string"
      ? { operation_id: row.operation_id }
      : {}),
    ...(row.arguments !== undefined ? { arguments: row.arguments } : {}),
    ...(row.result !== undefined ? { result: row.result } : {}),
  };
}

export function decodeWorkflow(value: unknown): WorkflowEntry[] {
  if (!Array.isArray(value) || value.length > 256)
    throw new Error("Invalid workflow");
  return value.map(decodeWorkflowEntry);
}

export function isWorkflow(value: unknown): value is WorkflowEntry[] {
  try {
    decodeWorkflow(value);
    return true;
  } catch {
    return false;
  }
}

type ReplyPart =
  | { kind: "text"; key: string; text: string }
  | { kind: "activity"; key: string; entries: WorkflowEntry[] };

export function replyParts(
  content: string,
  entries: WorkflowEntry[],
  streaming: boolean,
): ReplyPart[] {
  if (!entries.length)
    return content ? [{ kind: "text", key: "text:0", text: content }] : [];
  const chars = Array.from(content); // The server counts Unicode code points.
  const offsets: number[] = [];
  // A recovery can rewind the answer to a checkpoint. Keep earlier attempts
  // before the retried text. Legacy entries follow the available unsplit text.
  entries.reduceRight((next, entry, index) => {
    offsets[index] = Math.min(next, entry.answer_offset ?? chars.length);
    return offsets[index];
  }, Infinity);

  const parts: ReplyPart[] = [];
  let cursor = 0;
  function appendText(end: number) {
    if (end <= cursor) return;
    parts.push({
      kind: "text",
      key: `text:${cursor}`,
      text: chars.slice(cursor, end).join(""),
    });
    cursor = end;
  }
  for (const [index, entry] of entries.entries()) {
    const offset = offsets[index] ?? cursor;
    // Activity updates and text deltas travel independently during streaming.
    if (streaming && offset > chars.length) break;
    appendText(Math.min(offset, chars.length));
    const previous = parts.at(-1);
    if (previous?.kind === "activity") previous.entries.push(entry);
    else
      parts.push({
        kind: "activity",
        key: `activity:${entry.id}`,
        entries: [entry],
      });
  }
  appendText(chars.length);
  return parts;
}
