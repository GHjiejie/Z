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
