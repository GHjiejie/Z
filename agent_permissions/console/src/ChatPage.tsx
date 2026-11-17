import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Bot,
  Copy,
  History,
  MessageSquare,
  PanelLeftClose,
  Plus,
  RefreshCw,
  Square,
  Trash2,
  UserRound,
} from "lucide-react";
import { aborted, type ApiClient, type User } from "./api";
import { ErrorBanner, Modal, Spinner } from "./components";
import { ChatMarkdown } from "./ChatMarkdown";
import { ToolInteractionCard } from "./ToolInteractionCard";
import { ChatToolStatus } from "./ChatToolStatus";
import { AgentWorkflow } from "./AgentWorkflow";
import { useChat } from "./useChat";

function timeLabel(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function resizeComposer(element: HTMLTextAreaElement | null) {
  if (!element) return;
  element.style.height = "auto";
  element.style.height = `${Math.max(44, Math.min(element.scrollHeight, 144))}px`;
}

export function ChatPage({
  api,
  user,
  permissions,
  notify,
  onSettled,
}: {
  api: ApiClient;
  user: User;
  permissions: string[];
  notify: (text: string) => void;
  onSettled: () => void;
}) {
  const chat = useChat(api, user.id, onSettled);
  const active = chat.active;
  const [models, setModels] = useState<string[]>([]);
  const [modelError, setModelError] = useState<unknown>(null);
  const [modelLoading, setModelLoading] = useState(true);
  const [modelRevision, setModelRevision] = useState(0);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<unknown>(null);
  const [runReference, setRunReference] = useState("");
  const [opening, setOpening] = useState(false);
  const [openError, setOpenError] = useState<unknown>(null);
  const [historyOpen, setHistoryOpen] = useState(
    () => window.matchMedia("(min-width: 1101px)").matches,
  );
  const [showLatest, setShowLatest] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const historyToggle = useRef<HTMLButtonElement>(null);
  const following = useRef(true);
  const composing = useRef(false);
  const lastConversation = useRef(active?.id);
  const lastInteraction = useRef<string | undefined>(undefined);
  const interactionId = Object.values(chat.runs).find(
    (run) => run.conversation_id === active?.id && run.interaction,
  )?.interaction?.id;
  const model =
    active?.model && models.includes(active.model)
      ? active.model
      : (models[0] ?? "");
  const generating = chat.generatingId !== null;
  const canSend = Boolean(
    active?.draft.trim() &&
    model &&
    !modelLoading &&
    !modelError &&
    !generating &&
    !chat.pending &&
    !chat.recovering &&
    !chat.recoveryError,
  );

  useEffect(() => {
    const query = window.matchMedia("(min-width: 1101px)");
    const changed = (event: MediaQueryListEvent) =>
      setHistoryOpen(event.matches);
    query.addEventListener("change", changed);
    const resize = () => resizeComposer(composer.current);
    window.addEventListener("resize", resize);
    return () => {
      query.removeEventListener("change", changed);
      window.removeEventListener("resize", resize);
    };
  }, []);

  useLayoutEffect(() => {
    resizeComposer(composer.current);
  }, [active?.id, active?.draft, interactionId, historyOpen]);

  useEffect(() => {
    const controller = new AbortController();
    setModelLoading(true);
    setModelError(null);
    void api
      .chatModels(controller.signal)
      .then((values) => {
        if (!controller.signal.aborted) {
          if (!values.length) throw new Error("No models configured");
          setModels(values);
        }
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !aborted(error)) setModelError(error);
      })
      .finally(() => {
        if (!controller.signal.aborted) setModelLoading(false);
      });
    return () => controller.abort();
  }, [api, modelRevision]);

  useLayoutEffect(() => {
    if (lastConversation.current !== active?.id) {
      lastConversation.current = active?.id;
      following.current = true;
      setShowLatest(false);
    }
    if (interactionId) {
      if (lastInteraction.current !== interactionId && scroll.current) {
        const card = scroll.current.querySelector(".tool-interaction-card");
        if (card)
          scroll.current.scrollTop +=
            card.getBoundingClientRect().top -
            scroll.current.getBoundingClientRect().top -
            12;
        following.current = false;
        setShowLatest(false);
      }
      lastInteraction.current = interactionId;
      return;
    }
    if (lastInteraction.current) following.current = true;
    lastInteraction.current = undefined;
    if (following.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [active?.id, active?.messages, interactionId]);

  function latest() {
    following.current = true;
    setShowLatest(false);
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }
  function create() {
    chat.create();
    closeHistoryOnNarrow();
    requestAnimationFrame(() =>
      composer.current?.focus({ preventScroll: true }),
    );
  }
  function closeHistoryOnNarrow() {
    if (window.matchMedia("(max-width: 1100px)").matches) setHistoryOpen(false);
  }
  function send() {
    if (!canSend || composing.current) return;
    following.current = true;
    setShowLatest(false);
    void chat.send(model);
  }
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      notify("已复制回复");
    } catch {
      notify("当前浏览器无法复制，请选择文本后手动复制");
    }
  }
  if (!active) return null;
  const last = active.messages.at(-1);
  const status = generating
    ? last?.content
      ? "正在生成回复"
      : "正在处理请求"
    : last?.status === "waiting"
      ? "等待填写参数或确认操作"
      : last?.status === "cancelled"
        ? "操作已取消"
        : last?.status === "expired"
          ? "操作已过期"
          : last?.status === "failed"
            ? "回复未完成"
            : last?.status === "stopped"
              ? "已停止生成"
              : last
                ? "回复已完成"
                : "可以开始新会话";

  return (
    <section
      className="chat-page"
      aria-label="Agent 会话"
      onKeyDown={(event) => {
        if (event.key === "Escape" && historyOpen) {
          setHistoryOpen(false);
          historyToggle.current?.focus();
        }
      }}
    >
      <h1 className="sr-only">会话</h1>
      {chat.storageError && (
        <div className="info-note" role="status">
          {chat.storageError}
        </div>
      )}
      <div className={`chat-layout${historyOpen ? " has-history" : ""}`}>
        <aside
          className="chat-history panel"
          hidden={!historyOpen}
          id="chat-history"
          aria-label="会话记录"
        >
          <div className="chat-history-heading">
            <h2>会话记录</h2>
            <button
              className="icon-button"
              aria-label="收起会话记录"
              title="收起会话记录"
              onClick={() => {
                setHistoryOpen(false);
                historyToggle.current?.focus();
              }}
            >
              <PanelLeftClose size={18} />
            </button>
          </div>
          <button className="button secondary chat-new" onClick={create}>
            <Plus size={16} />
            新建会话
          </button>
          <nav className="conversation-list" aria-label="历史会话">
            {[...chat.history.conversations]
              .sort((a, b) => b.updatedAt.localeCompare(a.updatedAt))
              .map((item) => (
                <div
                  className={`conversation-row${item.id === active.id ? " selected" : ""}`}
                  key={item.id}
                >
                  <button
                    className="conversation-select"
                    onClick={() => {
                      chat.select(item.id);
                      closeHistoryOnNarrow();
                    }}
                    aria-current={item.id === active.id ? "page" : undefined}
                  >
                    <MessageSquare size={16} />
                    <span>
                      <strong>{item.title}</strong>
                      <small>{timeLabel(item.updatedAt)}</small>
                    </span>
                  </button>
                  <button
                    className="icon-button conversation-delete"
                    aria-label={`删除会话：${item.title}`}
                    title="删除会话"
                    onClick={() => {
                      setDeleteError(null);
                      setDeleteId(item.id);
                    }}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
          </nav>
          <details className="chat-open-run">
            <summary>打开已有任务</summary>
            <form
              onSubmit={(event) => {
                event.preventDefault();
                if (opening) return;
                setOpening(true);
                setOpenError(null);
                void chat
                  .importRun(runReference)
                  .then(() => {
                    setRunReference("");
                    closeHistoryOnNarrow();
                  })
                  .catch((error: unknown) => setOpenError(error))
                  .finally(() => setOpening(false));
              }}
            >
              <label htmlFor="chat-run-reference">任务标识</label>
              <input
                id="chat-run-reference"
                value={runReference}
                required
                maxLength={128}
                placeholder="粘贴 Chat 返回的任务标识"
                onChange={(event) => setRunReference(event.target.value)}
              />
              <button
                className="button secondary"
                disabled={opening || generating || !runReference.trim()}
              >
                {opening ? "正在读取…" : "打开任务"}
              </button>
              {Boolean(openError) && <ErrorBanner error={openError} />}
            </form>
          </details>
          <p className="chat-storage-note">
            记录仅保存在此浏览器
            <br />
            不同账号的会话相互隔离
          </p>
        </aside>
        <div className="chat-thread panel" onPointerDown={closeHistoryOnNarrow}>
          <header className="chat-thread-heading">
            <div className="chat-thread-title">
              <button
                ref={historyToggle}
                className="icon-button chat-history-toggle"
                aria-label="会话记录"
                title={historyOpen ? "收起会话记录" : "展开会话记录"}
                onPointerDown={(event) => event.stopPropagation()}
                onClick={() => setHistoryOpen((value) => !value)}
                aria-expanded={historyOpen}
                aria-controls="chat-history"
              >
                <History size={16} />
              </button>
              <Bot size={18} />
              <h2>{active.title}</h2>
            </div>
            <ChatToolStatus api={api} user={user} permissions={permissions} />
            <div className="chat-model-control">
              {modelLoading ? (
                <Spinner label="加载模型" />
              ) : modelError ? (
                <span className="danger-text">模型暂不可用</span>
              ) : (
                <select
                  aria-label="聊天模型"
                  value={model}
                  disabled={generating}
                  onChange={(event) => chat.setModel(event.target.value)}
                >
                  {models.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              )}
              <button
                className="icon-button"
                aria-label="刷新模型列表"
                title="刷新模型列表"
                disabled={modelLoading || generating}
                onClick={() => setModelRevision((value) => value + 1)}
              >
                <RefreshCw size={15} />
              </button>
            </div>
          </header>
          <div className="chat-message-region">
            <div
              className="chat-messages"
              role="log"
              aria-label="对话消息"
              aria-live="off"
              tabIndex={0}
              ref={scroll}
              onScroll={() => {
                const node = scroll.current;
                if (node) {
                  following.current =
                    node.scrollHeight - node.clientHeight - node.scrollTop < 64;
                  setShowLatest(!following.current);
                }
              }}
            >
              {!active.messages.length ? (
                <div className="chat-welcome">
                  <span className="chat-welcome-icon">
                    <MessageSquare size={26} />
                  </span>
                  <h2>从一个问题开始</h2>
                  <p>
                    可以查询当前账号的信息，也可以让 Agent
                    协助处理你有权限的业务
                  </p>
                  <div className="chat-suggestions">
                    {["查看我的账号信息", "我当前有哪些权限？"].map(
                      (suggestion) => (
                        <button
                          className="button secondary"
                          key={suggestion}
                          onClick={() => {
                            chat.setDraft(suggestion);
                            composer.current?.focus();
                          }}
                        >
                          {suggestion}
                          <ArrowUp size={15} />
                        </button>
                      ),
                    )}
                  </div>
                </div>
              ) : (
                active.messages.map((message, index) => (
                  <article
                    className={`chat-message chat-message-${message.role}`}
                    key={message.id}
                    aria-label={
                      message.role === "user" ? "你的消息" : "Agent 回复"
                    }
                  >
                    <span className="chat-message-avatar" aria-hidden="true">
                      {message.role === "user" ? (
                        <UserRound size={17} />
                      ) : (
                        <Bot size={18} />
                      )}
                    </span>
                    <div className="chat-message-body">
                      <div className="chat-message-meta">
                        <strong>
                          {message.role === "user"
                            ? user.display_name
                            : "Agent"}
                        </strong>
                        <time dateTime={message.createdAt}>
                          {timeLabel(message.createdAt)}
                        </time>
                      </div>
                      {message.role === "assistant" && (
                        <AgentWorkflow
                          entries={
                            (message.runId
                              ? chat.runs[message.runId]?.workflow
                              : undefined) ??
                            message.workflow ??
                            []
                          }
                        />
                      )}
                      <div className="chat-message-text">
                        {message.content ? (
                          message.role === "assistant" ? (
                            <ChatMarkdown content={message.content} />
                          ) : (
                            message.content
                          )
                        ) : message.status === "streaming" ? (
                          <Spinner label="正在处理请求" />
                        ) : message.status === "waiting" ? (
                          "请处理下方的工具操作。"
                        ) : message.status === "cancelled" ? (
                          "本次待执行操作已取消。"
                        ) : message.status === "expired" ? (
                          "本次待执行操作已过期。"
                        ) : (
                          "本次没有收到回复内容"
                        )}
                        {message.status === "streaming" && message.content && (
                          <span
                            className="chat-stream-cursor"
                            aria-hidden="true"
                          />
                        )}
                      </div>
                      {message.runId &&
                        chat.runs[message.runId]?.interaction &&
                        (() => {
                          const run = chat.runs[message.runId];
                          return run?.interaction ? (
                            <ToolInteractionCard
                              key={run.interaction.id}
                              api={api}
                              run={run}
                              busy={generating}
                              onSubmit={(payload) => chat.resume(run, payload)}
                              onCancel={() => chat.cancel(run)}
                              onRefresh={(requestId) =>
                                chat.refresh(run, requestId)
                              }
                              onRecover={chat.recover}
                            />
                          ) : null;
                        })()}
                      {message.runId &&
                        Boolean(
                          chat.runs[message.runId]?.executions.length,
                        ) && (
                          <div
                            className="tool-execution-summary"
                            aria-label="工具执行结果"
                          >
                            {chat.runs[message.runId]?.executions.map(
                              (execution) => (
                                <p key={execution.id}>
                                  {execution.status === "succeeded"
                                    ? "业务操作已完成"
                                    : execution.status === "failed"
                                      ? "业务操作未成功，请查看回复中的原因"
                                      : "业务操作执行结果待核对"}
                                </p>
                              ),
                            )}
                          </div>
                        )}
                      {message.role === "assistant" && (
                        <div className="chat-message-footer">
                          {message.status === "streaming" &&
                            message.content && (
                              <span role="status">正在生成</span>
                            )}
                          {message.status === "complete" && (
                            <small>
                              {message.finish === "length"
                                ? "已达到输出上限"
                                : "已完成"}
                              {message.usage
                                ? ` · ${message.usage.total_tokens.toLocaleString()} tokens`
                                : ""}
                            </small>
                          )}
                          {message.content && (
                            <button
                              className="icon-button"
                              aria-label="复制回复"
                              title="复制回复"
                              onClick={() => {
                                void copy(message.content);
                              }}
                            >
                              <Copy size={14} />
                            </button>
                          )}
                        </div>
                      )}
                      {(message.status === "stopped" ||
                        message.status === "failed") && (
                        <div
                          className={`chat-turn-notice${message.status === "failed" ? " failed" : ""}`}
                          role={
                            message.status === "failed" ? "alert" : "status"
                          }
                        >
                          <strong>
                            {message.status === "stopped"
                              ? "已停止生成"
                              : message.error || "回复未完成"}
                          </strong>
                          <p>
                            已保留收到的内容。部分业务操作可能已执行，请先查询确认结果。
                          </p>
                          {message.runId && (
                            <button
                              className="text-button"
                              disabled={generating || chat.recovering}
                              onClick={chat.recover}
                            >
                              读取最新任务状态
                            </button>
                          )}
                          {message.runId &&
                            ["interrupted", "failed"].includes(
                              chat.runs[message.runId]?.status ?? "",
                            ) && (
                              <button
                                className="text-button"
                                disabled={generating || chat.recovering}
                                onClick={() => {
                                  const run = chat.runs[message.runId ?? ""];
                                  if (run) void chat.continue(run);
                                }}
                              >
                                恢复此任务
                              </button>
                            )}
                          <button
                            className="text-button"
                            disabled={generating}
                            onClick={() => {
                              chat.setDraft(
                                active.messages[index - 1]?.content ?? "",
                              );
                              composer.current?.focus();
                            }}
                          >
                            编辑原消息
                          </button>
                        </div>
                      )}
                    </div>
                  </article>
                ))
              )}
            </div>
            {showLatest && !interactionId && (
              <button className="button secondary chat-jump" onClick={latest}>
                <ArrowDown size={15} />
                回到最新消息
              </button>
            )}
          </div>
          <form
            className={`chat-composer${interactionId && !generating ? " is-waiting" : ""}`}
            onSubmit={(event) => {
              event.preventDefault();
              send();
            }}
          >
            {Boolean(modelError) && (
              <ErrorBanner
                error={modelError}
                onRetry={() => setModelRevision((value) => value + 1)}
              />
            )}
            {Boolean(chat.recoveryError) && (
              <ErrorBanner error={chat.recoveryError} onRetry={chat.recover} />
            )}
            {chat.recovering && <p role="status">正在恢复任务状态…</p>}
            {chat.pending && !generating && (
              <p className="chat-pending-note" role="status">
                请先处理或取消当前操作，也可以新建会话。
                <button
                  className="text-button"
                  type="button"
                  onClick={chat.recover}
                >
                  刷新任务状态
                </button>
              </p>
            )}
            <label className="sr-only" htmlFor="chat-message-input">
              输入消息
            </label>
            <textarea
              id="chat-message-input"
              ref={composer}
              value={active.draft}
              rows={1}
              maxLength={16000}
              placeholder="输入消息，例如：查看我当前的账号信息"
              onChange={(event) => chat.setDraft(event.target.value)}
              onCompositionStart={() => {
                composing.current = true;
              }}
              onCompositionEnd={() => {
                composing.current = false;
              }}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing &&
                  event.keyCode !== 229 &&
                  canSend &&
                  !composing.current
                ) {
                  event.preventDefault();
                  send();
                }
              }}
            />
            <div className="chat-composer-footer">
              <span>Enter 发送 · Shift + Enter 换行</span>
              {generating ? (
                <button
                  className="button secondary"
                  type="button"
                  onClick={chat.stop}
                >
                  <Square size={14} />
                  停止生成
                </button>
              ) : (
                <button
                  className="button primary"
                  type="submit"
                  disabled={!canSend}
                >
                  <ArrowUp size={16} />
                  发送消息
                </button>
              )}
            </div>
          </form>
        </div>
      </div>
      <p className="sr-only" role="status" aria-live="polite">
        {status}
      </p>
      {deleteId && (
        <Modal
          title="删除会话"
          description="这会删除此浏览器中保存的消息和草稿"
          busy={deleting}
          destructive
          onClose={() => setDeleteId(null)}
          footer={
            <>
              <button
                className="button secondary"
                onClick={() => setDeleteId(null)}
              >
                取消
              </button>
              <button
                className="button danger"
                disabled={deleting}
                onClick={() => {
                  if (deleting) return;
                  setDeleting(true);
                  setDeleteError(null);
                  void chat
                    .remove(deleteId)
                    .then(() => setDeleteId(null))
                    .catch((error: unknown) => setDeleteError(error))
                    .finally(() => setDeleting(false));
                }}
              >
                确认删除
              </button>
            </>
          }
        >
          <p>
            待处理操作将被取消，已执行的业务操作会保留。
            {chat.generatingId === deleteId ? "当前回复也会停止生成。" : ""}
          </p>
          {Boolean(deleteError) && <ErrorBanner error={deleteError} />}
        </Modal>
      )}
    </section>
  );
}
