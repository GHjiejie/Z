import { useEffect, useRef, useState } from "react";
import { aborted, errorMessage, type ApiClient } from "./api";
import {
  chatId,
  completedContext,
  loadHistory,
  newConversation,
  saveHistory,
  type ChatHistory,
  type ChatMessage,
  type Conversation,
} from "./chatHistory";

interface Job {
  conversationId: string;
  messageId: string;
  controller: AbortController;
  text: string;
  frame: number | null;
}

export function useChat(api: ApiClient, userId: string, onSettled: () => void) {
  const [loaded] = useState(() => loadHistory(userId));
  const [history, setHistory] = useState(loaded.history);
  const latest = useRef(history);
  const mounted = useRef(true);
  const job = useRef<Job | null>(null);
  const storageTimer = useRef<number | null>(null);
  const [generatingId, setGeneratingId] = useState<string | null>(null);
  const [storageError, setStorageError] = useState(loaded.error);

  function commit(update: (value: ChatHistory) => ChatHistory) {
    latest.current = update(latest.current);
    if (mounted.current) setHistory(latest.current);
  }
  function change(id: string, update: (value: Conversation) => Conversation) {
    commit((value) => ({
      ...value,
      conversations: value.conversations.map((item) =>
        item.id === id ? update(item) : item,
      ),
    }));
  }
  function updateAnswer(current: Job, patch: Partial<ChatMessage>) {
    change(current.conversationId, (value) => ({
      ...value,
      updatedAt: new Date().toISOString(),
      messages: value.messages.map((item) =>
        item.id === current.messageId
          ? { ...item, content: current.text, ...patch }
          : item,
      ),
    }));
  }
  function stop() {
    const current = job.current;
    if (!current) return;
    job.current = null;
    current.controller.abort();
    if (current.frame !== null) cancelAnimationFrame(current.frame);
    updateAnswer(current, { status: "stopped" });
    if (mounted.current) setGeneratingId(null);
  }

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      stop();
      if (storageTimer.current !== null)
        window.clearTimeout(storageTimer.current);
      storageTimer.current = null;
      if (loaded.writable) saveHistory(userId, latest.current);
    };
    // The component is keyed by account; cleanup reads the current job and snapshot.
  }, [userId, loaded.writable]);

  useEffect(() => {
    if (!loaded.writable) return;
    const persist = () => {
      storageTimer.current = null;
      if (!saveHistory(userId, latest.current))
        setStorageError(
          "浏览器无法保存会话，请复制需要保留的内容；当前页面仍可继续聊天",
        );
    };
    if (generatingId) {
      // Keep saving during continuous deltas instead of postponing until idle.
      if (storageTimer.current === null)
        storageTimer.current = window.setTimeout(persist, 300);
    } else {
      if (storageTimer.current !== null)
        window.clearTimeout(storageTimer.current);
      persist();
    }
  }, [history, userId, loaded.writable, generatingId]);

  async function send(model: string) {
    if (job.current) return;
    const active = latest.current.conversations.find(
      (item) => item.id === latest.current.activeId,
    );
    const text = active?.draft.trim();
    if (!active || !text || !model) return;
    const now = new Date().toISOString();
    const user: ChatMessage = {
      id: chatId(),
      role: "user",
      content: text,
      status: "complete",
      createdAt: now,
    };
    const assistant: ChatMessage = {
      id: chatId(),
      role: "assistant",
      content: "",
      status: "streaming",
      createdAt: now,
    };
    const current: Job = {
      conversationId: active.id,
      messageId: assistant.id,
      controller: new AbortController(),
      text: "",
      frame: null,
    };
    job.current = current;
    setGeneratingId(active.id);
    change(active.id, (value) => ({
      ...value,
      model,
      draft: "",
      updatedAt: now,
      title: value.messages.length
        ? value.title
        : Array.from(text).slice(0, 28).join(""),
      messages: [...value.messages, user, assistant],
    }));
    try {
      const result = await api.streamChat(
        model,
        [...completedContext(active.messages), { role: "user", content: text }],
        current.controller.signal,
        (delta) => {
          if (job.current !== current || !mounted.current) return;
          current.text += delta;
          if (current.frame === null)
            current.frame = requestAnimationFrame(() => {
              current.frame = null;
              if (job.current === current && mounted.current)
                updateAnswer(current, {});
            });
        },
      );
      if (job.current === current && mounted.current)
        updateAnswer(current, {
          status: "complete",
          finish: result.finish,
          usage: result.usage,
        });
    } catch (error) {
      if (job.current === current && mounted.current)
        updateAnswer(current, {
          status: aborted(error) ? "stopped" : "failed",
          error: aborted(error) ? undefined : errorMessage(error),
        });
    } finally {
      if (current.frame !== null) cancelAnimationFrame(current.frame);
      if (job.current === current) {
        job.current = null;
        if (mounted.current) {
          setGeneratingId(null);
          onSettled();
        }
      }
    }
  }

  return {
    history,
    generatingId,
    storageError,
    send,
    stop,
    active: history.conversations.find((item) => item.id === history.activeId),
    setDraft: (draft: string) =>
      change(latest.current.activeId, (value) => ({ ...value, draft })),
    setModel: (model: string) =>
      change(latest.current.activeId, (value) => ({ ...value, model })),
    select: (id: string) => {
      stop();
      commit((value) => ({ ...value, activeId: id }));
    },
    create: () => {
      stop();
      const next = newConversation();
      commit((value) => ({
        ...value,
        activeId: next.id,
        conversations: [next, ...value.conversations],
      }));
    },
    remove: (id: string) => {
      if (job.current?.conversationId === id) stop();
      commit((value) => {
        const conversations = value.conversations.filter(
          (item) => item.id !== id,
        );
        if (!conversations.length) conversations.push(newConversation());
        return {
          ...value,
          conversations,
          activeId:
            value.activeId === id
              ? (conversations[0]?.id ?? "")
              : value.activeId,
        };
      });
    },
  };
}
