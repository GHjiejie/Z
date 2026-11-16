import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  Bot,
  Copy,
  History,
  MessageSquare,
  Plus,
  RefreshCw,
  Square,
  Trash2,
  UserRound,
} from "lucide-react";
import { aborted, type ApiClient, type User } from "./api";
import { ErrorBanner, Modal, Spinner } from "./components";
import { ChatMarkdown } from "./ChatMarkdown";
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

export function ChatPage({
  api,
  user,
  notify,
  onSettled,
}: {
  api: ApiClient;
  user: User;
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
  const [historyOpen, setHistoryOpen] = useState(false);
  const [showLatest, setShowLatest] = useState(false);
  const scroll = useRef<HTMLDivElement>(null);
  const composer = useRef<HTMLTextAreaElement>(null);
  const following = useRef(true);
  const composing = useRef(false);
  const lastConversation = useRef(active?.id);
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
    !generating,
  );

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
    if (following.current && scroll.current)
      scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [active?.id, active?.messages]);

  function latest() {
    following.current = true;
    setShowLatest(false);
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }
  function create() {
    chat.create();
    setHistoryOpen(false);
    requestAnimationFrame(() =>
      composer.current?.focus({ preventScroll: true }),
    );
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
    : last?.status === "failed"
      ? "回复未完成"
      : last?.status === "stopped"
        ? "已停止生成"
        : last
          ? "回复已完成"
          : "可以开始新会话";

  return (
    <section className="chat-page" aria-label="Agent 会话">
      <div className="page-heading">
        <div>
          <h1>会话</h1>
          <p>与 Agent 对话，按当前账号的权限查询和处理业务</p>
        </div>
        <button
          className="button secondary chat-mobile-history"
          onClick={() => setHistoryOpen((value) => !value)}
          aria-expanded={historyOpen}
          aria-controls="chat-history"
        >
          <History size={16} />
          会话记录
        </button>
      </div>
      {chat.storageError && (
        <div className="info-note" role="status">
          {chat.storageError}
        </div>
      )}
      <div className="chat-layout">
        <aside
          className={`chat-history panel${historyOpen ? " is-open" : ""}`}
          id="chat-history"
          aria-label="会话记录"
        >
          <div className="chat-history-heading">
            <h2>会话记录</h2>
            <button
              className="icon-button"
              aria-label="新建会话"
              title="新建会话"
              onClick={create}
            >
              <Plus size={18} />
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
                      setHistoryOpen(false);
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
                    onClick={() => setDeleteId(item.id)}
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ))}
          </nav>
          <p className="chat-storage-note">
            记录仅保存在此浏览器
            <br />
            不同账号的会话相互隔离
          </p>
        </aside>
        <div className="chat-thread panel">
          <header className="chat-thread-heading">
            <div>
              <Bot size={18} />
              <h2>{active.title}</h2>
            </div>
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
                      <div className="chat-message-text">
                        {message.content ? (
                          message.role === "assistant" ? (
                            <ChatMarkdown content={message.content} />
                          ) : (
                            message.content
                          )
                        ) : message.status === "streaming" ? (
                          <Spinner label="正在处理请求" />
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
            {showLatest && (
              <button className="button secondary chat-jump" onClick={latest}>
                <ArrowDown size={15} />
                回到最新消息
              </button>
            )}
          </div>
          <form
            className="chat-composer"
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
            <label className="sr-only" htmlFor="chat-message-input">
              输入消息
            </label>
            <textarea
              id="chat-message-input"
              ref={composer}
              value={active.draft}
              rows={3}
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
            <p className="chat-scope-note">
              Agent 仅能执行当前账号有权进行的操作
            </p>
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
          busy={false}
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
                onClick={() => {
                  chat.remove(deleteId);
                  setDeleteId(null);
                }}
              >
                确认删除
              </button>
            </>
          }
        >
          <p>
            删除会话不会撤销已执行的业务操作。
            {chat.generatingId === deleteId ? "当前回复也会停止生成。" : ""}
          </p>
        </Modal>
      )}
    </section>
  );
}
