import type {
  Artifact,
  ArtifactRef,
  Checkpoint,
  SessionState,
  SessionSummary,
} from "./types";

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

interface StreamEvent {
  type: "start" | "token" | "artifact_ready" | "state" | "error";
  content?: string;
  detail?: string;
  state?: SessionState;
  artifact?: ArtifactRef;
}

export interface StreamHandlers {
  onToken: (token: string) => void;
  onArtifact: (artifact: ArtifactRef) => void;
}

async function streamRequest(
  path: string,
  init: RequestInit,
  handlers: StreamHandlers,
): Promise<SessionState> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(
      response.status,
      typeof body?.detail === "string"
        ? body.detail
        : `请求失败（HTTP ${response.status}）`,
    );
  }
  if (!response.body) throw new ApiError(502, "服务器没有返回流式响应体");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalState: SessionState | null = null;

  const consume = (block: string) => {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) return;
    const event = JSON.parse(data) as StreamEvent;
    if (event.type === "token" && event.content) handlers.onToken(event.content);
    if (event.type === "artifact_ready" && event.artifact) {
      handlers.onArtifact(event.artifact);
    }
    if (event.type === "state" && event.state) finalState = event.state;
    if (event.type === "error") throw new ApiError(502, event.detail || "流式执行失败");
  };

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      consume(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) consume(buffer);
  if (!finalState) throw new ApiError(502, "流式响应结束但缺少最终状态");
  return finalState;
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

  streamMessage: (
    threadId: string,
    content: string,
    handlers: StreamHandlers,
  ) =>
    streamRequest(
      `${sessionPath(threadId)}/messages/stream`,
      {
        method: "POST",
        body: JSON.stringify({ content }),
      },
      handlers,
    ),

  decideApproval: (threadId: string, approved: boolean) =>
    request<SessionState>(`${sessionPath(threadId)}/approval`, {
      method: "POST",
      body: JSON.stringify({ approved }),
    }),

  streamApproval: (
    threadId: string,
    approved: boolean,
    handlers: StreamHandlers,
  ) =>
    streamRequest(
      `${sessionPath(threadId)}/approval/stream`,
      {
        method: "POST",
        body: JSON.stringify({ approved }),
      },
      handlers,
    ),

  retry: (threadId: string) =>
    request<SessionState>(`${sessionPath(threadId)}/retry`, {
      method: "POST",
    }),

  streamRetry: (threadId: string, handlers: StreamHandlers) =>
    streamRequest(
      `${sessionPath(threadId)}/retry/stream`,
      { method: "POST" },
      handlers,
    ),

  artifacts: (threadId: string) =>
    request<ArtifactRef[]>(`${sessionPath(threadId)}/artifacts`),

  artifact: (threadId: string, artifactId: string) =>
    request<Artifact>(
      `${sessionPath(threadId)}/artifacts/${encodeURIComponent(artifactId)}`,
    ),

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
