import type { ChatMessageInput, ChatUsage } from "./chatStream";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "complete" | "streaming" | "stopped" | "failed";
  createdAt: string;
  error?: string;
  finish?: "stop" | "length";
  usage?: ChatUsage;
}
export interface Conversation {
  id: string;
  title: string;
  model: string;
  draft: string;
  createdAt: string;
  updatedAt: string;
  messages: ChatMessage[];
}
export interface ChatHistory {
  version: 1;
  activeId: string;
  conversations: Conversation[];
}
export function chatId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}
export function newConversation(): Conversation {
  const now = new Date().toISOString();
  return {
    id: chatId(),
    title: "新会话",
    model: "",
    draft: "",
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
}
export function historyKey(userId: string): string {
  return `agent-console-chat-v1:${userId}`;
}

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function message(value: unknown): value is ChatMessage {
  return (
    record(value) &&
    typeof value.id === "string" &&
    (value.role === "user" || value.role === "assistant") &&
    typeof value.content === "string" &&
    ["complete", "streaming", "stopped", "failed"].includes(
      String(value.status),
    ) &&
    typeof value.createdAt === "string" &&
    Number.isFinite(Date.parse(value.createdAt)) &&
    (value.error === undefined || typeof value.error === "string") &&
    (value.finish === undefined ||
      value.finish === "stop" ||
      value.finish === "length") &&
    (value.usage === undefined ||
      (record(value.usage) &&
        [
          value.usage.prompt_tokens,
          value.usage.completion_tokens,
          value.usage.total_tokens,
        ].every(
          (item) =>
            typeof item === "number" && Number.isSafeInteger(item) && item >= 0,
        )))
  );
}
function conversation(value: unknown): value is Conversation {
  return (
    record(value) &&
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    typeof value.model === "string" &&
    typeof value.draft === "string" &&
    typeof value.createdAt === "string" &&
    Number.isFinite(Date.parse(value.createdAt)) &&
    typeof value.updatedAt === "string" &&
    Number.isFinite(Date.parse(value.updatedAt)) &&
    Array.isArray(value.messages) &&
    value.messages.every(message) &&
    new Set(value.messages.map((item) => item.id)).size ===
      value.messages.length
  );
}

export function loadHistory(userId: string): {
  history: ChatHistory;
  writable: boolean;
  error: string;
} {
  const empty = newConversation();
  const fallback: ChatHistory = {
    version: 1,
    activeId: empty.id,
    conversations: [empty],
  };
  try {
    const raw = localStorage.getItem(historyKey(userId));
    if (!raw) return { history: fallback, writable: true, error: "" };
    const parsed: unknown = JSON.parse(raw);
    if (
      !record(parsed) ||
      parsed.version !== 1 ||
      typeof parsed.activeId !== "string" ||
      !Array.isArray(parsed.conversations) ||
      !parsed.conversations.every(conversation) ||
      new Set(parsed.conversations.map((item) => item.id)).size !==
        parsed.conversations.length
    )
      throw new Error("Invalid history");
    const conversations = parsed.conversations.map((item) => ({
      ...item,
      messages: item.messages.map((entry) =>
        entry.status === "streaming"
          ? { ...entry, status: "stopped" as const }
          : entry,
      ),
    }));
    if (!conversations.length)
      return { history: fallback, writable: true, error: "" };
    return {
      history: {
        version: 1,
        conversations,
        activeId: conversations.some((item) => item.id === parsed.activeId)
          ? parsed.activeId
          : (conversations[0]?.id ?? empty.id),
      },
      writable: true,
      error: "",
    };
  } catch {
    return {
      history: fallback,
      writable: false,
      error: "本地记录无法读取，本次会话仅保留在当前页面，原有记录未被覆盖",
    };
  }
}

export function saveHistory(userId: string, history: ChatHistory): boolean {
  try {
    localStorage.setItem(historyKey(userId), JSON.stringify(history));
    return true;
  } catch {
    return false;
  }
}

export function completedContext(messages: ChatMessage[]): ChatMessageInput[] {
  const turns: ChatMessageInput[][] = [];
  for (let i = 0; i < messages.length - 1; i++) {
    const user = messages[i];
    const answer = messages[i + 1];
    if (
      user?.role === "user" &&
      answer?.role === "assistant" &&
      answer.status === "complete"
    ) {
      turns.push([
        { role: "user", content: user.content },
        { role: "assistant", content: answer.content },
      ]);
      i++;
    }
  }
  return turns.slice(-60).flat();
}
