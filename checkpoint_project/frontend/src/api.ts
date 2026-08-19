import type { Checkpoint, SessionState, SessionSummary } from "./types";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = body?.detail;
    const message =
      typeof detail === "string"
        ? detail
        : `请求失败（HTTP ${response.status}）`;
    throw new ApiError(response.status, message);
  }
  return body as T;
}

const sessionPath = (threadId: string) =>
  `/api/sessions/${encodeURIComponent(threadId)}`;

export const api = {
  listSessions: () => request<SessionSummary[]>("/api/sessions"),

  createSession: (threadId?: string) =>
    request<SessionState>("/api/sessions", {
      method: "POST",
      body: JSON.stringify({ thread_id: threadId || null }),
    }),

  getSession: (threadId: string) =>
    request<SessionState>(sessionPath(threadId)),

  sendMessage: (threadId: string, content: string) =>
    request<SessionState>(`${sessionPath(threadId)}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),

  decideApproval: (threadId: string, approved: boolean) =>
    request<SessionState>(`${sessionPath(threadId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),

  retry: (threadId: string) =>
    request<SessionState>(`${sessionPath(threadId)}/retry`, {
      method: "POST",
    }),

  checkpoints: (threadId: string) =>
    request<Checkpoint[]>(`${sessionPath(threadId)}/checkpoints`),

  fork: (threadId: string, checkpointId: string, newThreadId?: string) =>
    request<SessionState>(`${sessionPath(threadId)}/fork`, {
      method: "POST",
      body: JSON.stringify({
        checkpoint_id: checkpointId,
        new_thread_id: newThreadId || null,
      }),
    }),
};
