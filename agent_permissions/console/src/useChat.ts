import { useEffect, useRef, useState } from "react";
import { aborted, errorMessage, type ApiClient } from "./api";
import type { ChatRun, RunEvent, ToolSubmission } from "./chatRuns";
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
  runId?: string;
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
  const [runs, setRuns] = useState<Record<string, ChatRun>>({});
  const currentRuns = useRef<Record<string, ChatRun>>({});
  const [recoveryError, setRecoveryError] = useState<unknown>(null);
  const [recovering, setRecovering] = useState(false);
  const [recoveryRevision, setRecoveryRevision] = useState(0);

  function applyRun(run: ChatRun) {
    if (!mounted.current) return;
    if ((currentRuns.current[run.id]?.version ?? 0) > run.version) return;
    currentRuns.current = { ...currentRuns.current, [run.id]: run };
    setRuns(currentRuns.current);
    const status: ChatMessage["status"] =
      run.status === "completed"
        ? "complete"
        : run.status === "waiting"
          ? "waiting"
          : run.status === "running"
            ? "streaming"
            : run.status === "cancelled"
              ? "cancelled"
              : run.status === "expired"
                ? "expired"
                : run.status === "interrupted"
                  ? "stopped"
                  : "failed";
    change(run.conversation_id, (value) => ({
      ...value,
      messages: (value.messages.some(
        (message) =>
          message.id === run.client_request_id || message.runId === run.id,
      )
        ? value.messages
        : [
            ...value.messages,
            {
              id: run.client_request_id,
              role: "assistant" as const,
              content: "",
              status: "stopped" as const,
              createdAt: new Date().toISOString(),
            },
          ]
      ).map((message) =>
        message.id === run.client_request_id || message.runId === run.id
          ? {
              ...message,
              runId: run.id,
              content: run.content,
              status,
              usage: run.usage ?? undefined,
              finish: run.finish,
              error: run.error?.message,
            }
          : message,
      ),
    }));
  }

  useEffect(() => {
    const controller = new AbortController();
    const conversation = history.activeId;
    if (job.current?.conversationId === conversation) return;
    setRecovering(true);
    setRecoveryError(null);
    void api
      .chatRuns(conversation, controller.signal)
      .then((values) => {
        if (
          controller.signal.aborted ||
          !mounted.current ||
          job.current?.conversationId === conversation
        )
          return;
        [...values].reverse().forEach(applyRun);
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !aborted(error))
          setRecoveryError(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setRecovering(false);
      });
    return () => controller.abort();
    // Changes to a message do not launch competing recovery requests.
  }, [api, history.activeId, recoveryRevision]);

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
    const previousRun = current.runId
      ? currentRuns.current[current.runId]
      : undefined;
    if (previousRun) {
      const paused = {
        ...previousRun,
        status: "interrupted" as const,
        content: current.text,
      };
      currentRuns.current = { ...currentRuns.current, [paused.id]: paused };
      if (mounted.current) setRuns(currentRuns.current);
    }
    if (mounted.current) setGeneratingId(null);
    if (current.runId && mounted.current) {
      void api
        .cancelChatRun(current.runId)
        .then((run) => {
          if (mounted.current) applyRun(run);
        })
        .catch((error: unknown) => {
          if (
            mounted.current &&
            !aborted(error) &&
            !(
              error instanceof Error &&
              "status" in error &&
              error.status === 404
            )
          )
            setRecoveryError(error);
        });
    }
  }

  function receive(current: Job, event: RunEvent) {
    if (job.current !== current || !mounted.current) return;
    current.runId = event.run_id;
    if (event.text !== undefined) {
      current.text += event.text;
      if (current.frame === null)
        current.frame = requestAnimationFrame(() => {
          current.frame = null;
          if (job.current === current && mounted.current)
            updateAnswer(current, { runId: current.runId });
        });
    }
    if (event.run) {
      if (current.frame !== null) cancelAnimationFrame(current.frame);
      current.frame = null;
      current.text = event.run.content;
      applyRun(event.run);
    }
  }

  function settled(current: Job) {
    if (current.frame !== null) cancelAnimationFrame(current.frame);
    if (job.current === current) {
      job.current = null;
      if (mounted.current) {
        setGeneratingId(null);
        onSettled();
      }
    }
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
    if (
      !active ||
      !text ||
      !model ||
      recovering ||
      Object.values(runs).some(
        (run) =>
          run.conversation_id === active.id &&
          ["waiting", "running"].includes(run.status),
      )
    )
      return;
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
      await api.streamRun(
        "/chat/runs",
        {
          model,
          messages: [
            ...completedContext(active.messages),
            { role: "user", content: text },
          ],
          conversation_id: active.id,
          client_request_id: assistant.id,
        },
        current.controller.signal,
        (event) => receive(current, event),
      );
    } catch (error) {
      if (job.current === current && mounted.current)
        updateAnswer(current, {
          status: aborted(error) ? "stopped" : "failed",
          error: aborted(error) ? undefined : errorMessage(error),
        });
    } finally {
      settled(current);
    }
  }

  async function continueRun(run: ChatRun, path: string, payload: object) {
    if (job.current) return;
    const conversation = latest.current.conversations.find(
      (item) => item.id === run.conversation_id,
    );
    const message = conversation?.messages.find(
      (item) => item.runId === run.id,
    );
    if (!message) return;
    const current: Job = {
      conversationId: run.conversation_id,
      messageId: message.id,
      runId: run.id,
      controller: new AbortController(),
      text: message.content,
      frame: null,
    };
    job.current = current;
    setGeneratingId(run.conversation_id);
    try {
      await api.streamRun(path, payload, current.controller.signal, (event) =>
        receive(current, event),
      );
    } catch (error) {
      if (
        job.current === current &&
        currentRuns.current[run.id]?.status === "running"
      ) {
        updateAnswer(current, {
          status: "stopped",
          error: errorMessage(error),
        });
        setRecoveryError(error);
      }
      throw error;
    } finally {
      settled(current);
    }
  }

  return {
    history,
    generatingId,
    storageError,
    recoveryError,
    recovering,
    runs,
    resume: (run: ChatRun, payload: ToolSubmission) =>
      run.interaction
        ? continueRun(
            run,
            `/chat/runs/${encodeURIComponent(run.id)}/interactions/${encodeURIComponent(run.interaction.id)}/resume`,
            payload,
          )
        : Promise.resolve(),
    continue: async (run: ChatRun) => {
      setRecoveryError(null);
      try {
        await continueRun(
          run,
          `/chat/runs/${encodeURIComponent(run.id)}/recover`,
          {},
        );
      } catch (error) {
        if (!aborted(error)) setRecoveryError(error);
      }
    },
    importRun: async (id: string) => {
      if (job.current) return;
      const run = await api.chatRun(id.trim());
      if (!mounted.current) return;
      commit((value) => {
        const existing = value.conversations.find(
          (item) => item.id === run.conversation_id,
        );
        const next = existing ?? {
          ...newConversation(),
          id: run.conversation_id,
          title: "恢复的任务",
        };
        if (
          !next.messages.some(
            (item) =>
              item.runId === run.id || item.id === run.client_request_id,
          )
        )
          next.messages = [
            ...next.messages,
            {
              id: run.client_request_id,
              runId: run.id,
              role: "assistant",
              content: run.content,
              status: "stopped",
              createdAt: new Date().toISOString(),
            },
          ];
        return {
          ...value,
          activeId: next.id,
          conversations: [
            next,
            ...value.conversations.filter((item) => item.id !== next.id),
          ],
        };
      });
      applyRun(run);
    },
    recover: () => setRecoveryRevision((value) => value + 1),
    cancel: async (run: ChatRun) => {
      applyRun(await api.cancelChatRun(run.id));
      onSettled();
    },
    refresh: async (run: ChatRun, requestId: string) => {
      if (run.interaction)
        applyRun(
          await api.refreshInteraction(
            run.id,
            run.interaction.id,
            run.interaction.version,
            requestId,
          ),
        );
    },
    pending: Object.values(runs).some(
      (run) =>
        run.conversation_id === history.activeId &&
        ["waiting", "running"].includes(run.status),
    ),
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
    remove: async (id: string) => {
      if (job.current?.conversationId === id) stop();
      const latestRuns = await api.chatRuns(id, new AbortController().signal);
      for (const run of latestRuns) {
        if (
          run.conversation_id === id &&
          ["waiting", "running", "interrupted"].includes(run.status)
        )
          applyRun(await api.cancelChatRun(run.id));
      }
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
