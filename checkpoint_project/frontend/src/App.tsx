import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type Dispatch,
  type SetStateAction,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, type StreamProgress } from "./api";
import {
  BranchIcon,
  BrowserIcon,
  ClockIcon,
  CloseIcon,
  CodeIcon,
  DatabaseIcon,
  FileIcon,
  MenuIcon,
  PlusIcon,
  RefreshIcon,
  SendIcon,
  ShieldIcon,
  SparkIcon,
} from "./icons";
import type {
  ApprovalPayload,
  Artifact,
  ArtifactRef,
  ChatMessage,
  Checkpoint,
  MessageContent,
  SessionState,
  SessionSummary,
} from "./types";

const ACTIVE_SESSION_KEY = "checkpoint-studio-active-session";

interface RunProgressView extends StreamProgress {
  startedAt: number;
  lastEventAt: number;
  connected: boolean;
}

export default function App() {
  const initialized = useRef(false);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [chat, setChat] = useState<SessionState | null>(null);
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [composer, setComposer] = useState("");
  const [busy, setBusy] = useState<string | null>("boot");
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [timelineOpen, setTimelineOpen] = useState(false);
  const [forkTarget, setForkTarget] = useState<Checkpoint | null>(null);
  const [forkName, setForkName] = useState("");
  const [runProgress, setRunProgress] = useState<RunProgressView | null>(null);

  const loadSession = useCallback(async (threadId: string) => {
    const [state, history] = await Promise.all([
      api.getSession(threadId),
      api.checkpoints(threadId),
    ]);
    setActiveId(threadId);
    setChat(state);
    setCheckpoints(history);
    localStorage.setItem(ACTIVE_SESSION_KEY, threadId);
  }, []);

  const refreshSessions = useCallback(async () => {
    const items = await api.listSessions();
    setSessions(items);
    return items;
  }, []);

  useEffect(() => {
    if (initialized.current) return;
    initialized.current = true;

    const boot = async () => {
      setBusy("boot");
      try {
        let items = await refreshSessions();
        if (!items.length) {
          const created = await api.createSession("main");
          items = await refreshSessions();
          setChat(created);
        }
        const saved = localStorage.getItem(ACTIVE_SESSION_KEY);
        const target =
          items.find((item) => item.thread_id === saved)?.thread_id ??
          items[0].thread_id;
        await loadSession(target);
      } catch (reason) {
        setError(errorMessage(reason));
      } finally {
        setBusy(null);
      }
    };
    void boot();
  }, [loadSession, refreshSessions]);

  const selectSession = async (threadId: string) => {
    if (threadId === activeId) {
      setSidebarOpen(false);
      return;
    }
    setBusy("session");
    setError(null);
    try {
      await loadSession(threadId);
      setSidebarOpen(false);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const createSession = async () => {
    setBusy("new");
    setError(null);
    try {
      const created = await api.createSession();
      await refreshSessions();
      setActiveId(created.thread_id);
      setChat(created);
      setCheckpoints([]);
      setSidebarOpen(false);
      localStorage.setItem(ACTIVE_SESSION_KEY, created.thread_id);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const refreshCurrent = async () => {
    if (!activeId) return;
    setBusy("refresh");
    setError(null);
    try {
      await Promise.all([loadSession(activeId), refreshSessions()]);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const sendMessage = async (event?: FormEvent) => {
    event?.preventDefault();
    const content = composer.trim();
    if (!content || !activeId || !chat || busy || chat.status !== "idle") return;

    const optimistic: ChatMessage = {
      id: `optimistic-${Date.now()}`,
      type: "human",
      content,
    };
    setComposer("");
    setBusy("message");
    setRunProgress(beginRunProgress("正在连接后端并提交请求…"));
    setError(null);
    setChat({ ...chat, messages: [...chat.messages, optimistic] });
    const streamHandlers = createStreamHandlers(setChat, setRunProgress);
    try {
      const state = await api.streamMessage(activeId, content, streamHandlers);
      setChat(state);
      const [history] = await Promise.all([
        api.checkpoints(activeId),
        refreshSessions(),
      ]);
      setCheckpoints(history);
    } catch (reason) {
      setError(errorMessage(reason));
      try {
        await loadSession(activeId);
      } catch {
        // Keep the original, more useful error.
      }
    } finally {
      setRunProgress(null);
      setBusy(null);
    }
  };

  const decideApproval = async (approved: boolean) => {
    if (!activeId || busy) return;
    setBusy(approved ? "approve" : "reject");
    setRunProgress(
      beginRunProgress(
        approved ? "正在提交确认并创建实时预览…" : "正在提交选择并生成代码回答…",
      ),
    );
    setError(null);
    setChat((current) =>
      current
        ? { ...current, status: "idle", pending_approvals: [] }
        : current,
    );
    const streamHandlers = createStreamHandlers(setChat, setRunProgress);
    try {
      const state = await api.streamApproval(activeId, approved, streamHandlers);
      setChat(state);
      const [history] = await Promise.all([
        api.checkpoints(activeId),
        refreshSessions(),
      ]);
      setCheckpoints(history);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setRunProgress(null);
      setBusy(null);
    }
  };

  const retry = async () => {
    if (!activeId || busy) return;
    setBusy("retry");
    setRunProgress(beginRunProgress("正在连接后端并恢复执行…"));
    setError(null);
    const streamHandlers = createStreamHandlers(setChat, setRunProgress);
    try {
      const state = await api.streamRetry(activeId, streamHandlers);
      setChat(state);
      setCheckpoints(await api.checkpoints(activeId));
      await refreshSessions();
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setRunProgress(null);
      setBusy(null);
    }
  };

  const openFork = (checkpoint: Checkpoint) => {
    setForkTarget(checkpoint);
    setForkName(`branch-${Date.now().toString(36)}`);
  };

  const createFork = async (event: FormEvent) => {
    event.preventDefault();
    if (!activeId || !forkTarget || busy) return;
    setBusy("fork");
    setError(null);
    try {
      const branch = await api.fork(
        activeId,
        forkTarget.checkpoint_id,
        forkName.trim() || undefined,
      );
      setForkTarget(null);
      setActiveId(branch.thread_id);
      setChat(branch);
      setCheckpoints(await api.checkpoints(branch.thread_id));
      await refreshSessions();
      localStorage.setItem(ACTIVE_SESSION_KEY, branch.thread_id);
      setTimelineOpen(false);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setBusy(null);
    }
  };

  const approval = chat?.pending_approvals[0]?.payload;
  const isLoading = busy === "boot";

  return (
    <div className="app-shell">
      <div
        className={`mobile-scrim ${sidebarOpen || timelineOpen ? "visible" : ""}`}
        onClick={() => {
          setSidebarOpen(false);
          setTimelineOpen(false);
        }}
      />

      <SessionSidebar
        sessions={sessions}
        activeId={activeId}
        open={sidebarOpen}
        busy={Boolean(busy)}
        onClose={() => setSidebarOpen(false)}
        onCreate={() => void createSession()}
        onSelect={(id) => void selectSession(id)}
      />

      <main className="chat-column">
        <ChatHeader
          chat={chat}
          busy={busy}
          runProgress={runProgress}
          onOpenSidebar={() => setSidebarOpen(true)}
          onOpenTimeline={() => setTimelineOpen(true)}
          onRefresh={() => void refreshCurrent()}
        />

        {error && (
          <div className="error-toast" role="alert">
            <span>{error}</span>
            <button onClick={() => setError(null)} aria-label="关闭错误提示">
              <CloseIcon />
            </button>
          </div>
        )}

        {isLoading ? (
          <LoadingState />
        ) : (
          <>
            <MessageList
              messages={chat?.messages ?? []}
              threadId={chat?.thread_id ?? null}
              busy={["message", "approve", "reject", "retry"].includes(busy ?? "")}
              runProgress={runProgress}
            />

            {approval && (
              <ApprovalCard
                payload={approval}
                busy={busy}
                onDecision={(approved) => void decideApproval(approved)}
              />
            )}

            {chat?.status === "recoverable" && (
              <div className="recovery-card">
                <div>
                  <strong>执行在 checkpoint 处暂停</strong>
                  <span>上一次节点未完成，可从最后成功状态继续。</span>
                </div>
                <button onClick={() => void retry()} disabled={Boolean(busy)}>
                  <RefreshIcon />
                  重新执行
                </button>
              </div>
            )}

            <Composer
              value={composer}
              disabled={Boolean(busy) || !chat || chat.status !== "idle"}
              waitingApproval={chat?.status === "waiting_approval"}
              onChange={setComposer}
              onSubmit={(event) => void sendMessage(event)}
            />
          </>
        )}
      </main>

      <CheckpointPanel
        checkpoints={checkpoints}
        currentId={chat?.checkpoint_id ?? null}
        open={timelineOpen}
        onClose={() => setTimelineOpen(false)}
        onFork={openFork}
      />

      {forkTarget && (
        <ForkDialog
          checkpoint={forkTarget}
          name={forkName}
          busy={busy === "fork"}
          onNameChange={setForkName}
          onClose={() => setForkTarget(null)}
          onSubmit={(event) => void createFork(event)}
        />
      )}
    </div>
  );
}

interface SessionSidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  open: boolean;
  busy: boolean;
  onClose: () => void;
  onCreate: () => void;
  onSelect: (id: string) => void;
}

function SessionSidebar({
  sessions,
  activeId,
  open,
  busy,
  onClose,
  onCreate,
  onSelect,
}: SessionSidebarProps) {
  return (
    <aside className={`session-sidebar ${open ? "mobile-open" : ""}`}>
      <div className="brand-row">
        <div className="brand-mark"><span /><span /><span /></div>
        <div>
          <strong>Checkpoint</strong>
          <span>Studio</span>
        </div>
        <button className="icon-button mobile-only" onClick={onClose} aria-label="关闭会话栏">
          <CloseIcon />
        </button>
      </div>

      <button className="new-session-button" onClick={onCreate} disabled={busy}>
        <PlusIcon />
        新建会话
      </button>

      <div className="sidebar-section-label">
        <span>会话</span>
        <small>{sessions.length}</small>
      </div>

      <nav className="session-list" aria-label="会话列表">
        {sessions.map((session) => (
          <button
            key={session.thread_id}
            className={`session-item ${session.thread_id === activeId ? "active" : ""}`}
            onClick={() => onSelect(session.thread_id)}
          >
            <span className={`status-dot ${session.status}`} />
            <span className="session-copy">
              <strong>{session.thread_id}</strong>
              <small>{sessionPreview(session)}</small>
              {session.source_thread_id && (
                <em><BranchIcon /> 来自 {session.source_thread_id}</em>
              )}
            </span>
            <time>{shortDate(session.created_at)}</time>
          </button>
        ))}
      </nav>

      <div className="storage-note">
        <DatabaseIcon />
        <div>
          <strong>SQLite 本地持久化</strong>
          <span>对话与 checkpoint 自动保存</span>
        </div>
      </div>
    </aside>
  );
}

interface ChatHeaderProps {
  chat: SessionState | null;
  busy: string | null;
  runProgress: RunProgressView | null;
  onOpenSidebar: () => void;
  onOpenTimeline: () => void;
  onRefresh: () => void;
}

function ChatHeader({
  chat,
  busy,
  runProgress,
  onOpenSidebar,
  onOpenTimeline,
  onRefresh,
}: ChatHeaderProps) {
  return (
    <header className="chat-header">
      <button className="icon-button mobile-only" onClick={onOpenSidebar} aria-label="打开会话栏">
        <MenuIcon />
      </button>
      <div className="header-title">
        <div className="eyebrow">ACTIVE THREAD</div>
        <h1>{chat?.thread_id ?? "正在连接…"}</h1>
      </div>
      <div className="header-metrics">
        <span className={`status-pill ${runProgress ? "working" : chat?.status ?? "idle"}`}>
          <i />
          {runProgress ? "后端处理中" : statusLabel(chat?.status)}
        </span>
        <span className="turn-pill">{chat?.turn_count ?? 0} 次模型执行</span>
      </div>
      <div className="header-actions">
        <button className="icon-button" onClick={onRefresh} disabled={Boolean(busy)} aria-label="刷新">
          <RefreshIcon className={busy === "refresh" ? "spin" : ""} />
        </button>
        <button className="timeline-button" onClick={onOpenTimeline} aria-label="打开 checkpoint 时间线">
          <ClockIcon />
          <span>时间线</span>
        </button>
      </div>
    </header>
  );
}

function MessageList({
  messages,
  threadId,
  busy,
  runProgress,
}: {
  messages: ChatMessage[];
  threadId: string | null;
  busy: boolean;
  runProgress: RunProgressView | null;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const frame = requestAnimationFrame(() => {
      endRef.current?.scrollIntoView({
        behavior: busy ? "auto" : "smooth",
        block: "end",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [messages, busy]);

  return (
    <section className="message-scroll" aria-live="polite">
      <div className="message-inner">
        {!messages.length ? <EmptyConversation /> : null}
        {messages.map((message, index) => (
          <MessageBubble
            key={message.id ?? `${message.type}-${index}`}
            message={message}
            threadId={threadId}
            runProgress={isStreamingMessage(message) ? runProgress : null}
          />
        ))}
        {runProgress && !messages.some(isStreamingMessage) && (
          <div className="message-row ai-row run-activity-row">
            <div className="avatar assistant-avatar"><SparkIcon /></div>
            <div className="message-bubble ai-bubble">
              <div className="message-author">Checkpoint Assistant</div>
              <RunActivity progress={runProgress} />
            </div>
          </div>
        )}
        {busy && !runProgress && !messages.some(isStreamingMessage) && (
          <div className="message-row ai-row">
            <div className="avatar assistant-avatar"><SparkIcon /></div>
            <div className="thinking-bubble"><span /><span /><span /></div>
          </div>
        )}
        <div ref={endRef} />
      </div>
    </section>
  );
}

function EmptyConversation() {
  return (
    <div className="empty-conversation">
      <div className="empty-icon"><SparkIcon /></div>
      <span className="empty-kicker">DURABLE CONVERSATION</span>
      <h2>从这里开始一条可回溯的对话</h2>
      <p>每一步都会写入 SQLite。你可以随时查看历史、批准文件操作，或从任意节点开启分支。</p>
      <div className="suggestion-row">
        <span>试试：</span>
        <code>记住我喜欢简洁的方案</code>
        <code>列出工作区文件</code>
        <code>生成一个可预览的登录页面</code>
      </div>
    </div>
  );
}

function MessageBubble({
  message,
  threadId,
  runProgress,
}: {
  message: ChatMessage;
  threadId: string | null;
  runProgress: RunProgressView | null;
}) {
  if (message.type === "tool") {
    if (message.artifact?.kind === "html" && threadId) {
      return (
        <HtmlArtifactCard
          threadId={threadId}
          reference={message.artifact}
          failed={message.tool_status === "error"}
        />
      );
    }
    return (
      <div className={`tool-result ${message.tool_status === "error" ? "error" : ""}`}>
        <FileIcon />
        <div>
          <span>{message.name ?? "工具结果"}</span>
          <p>{contentText(message.content)}</p>
        </div>
      </div>
    );
  }

  const human = message.type === "human";
  const streaming = isStreamingMessage(message);
  const { thought, answer, thinking } = splitThought(contentText(message.content));
  const toolCalls = message.tool_calls ?? [];
  return (
    <div className={`message-row ${human ? "human-row" : "ai-row"}`}>
      {!human && <div className="avatar assistant-avatar"><SparkIcon /></div>}
      <div className={`message-bubble ${human ? "human-bubble" : "ai-bubble"} ${streaming ? "streaming" : ""}`}>
        {!human && <div className="message-author">Checkpoint Assistant</div>}
        {thought && (
          <details className="thought-block" open={thinking}>
            <summary>{thinking ? "模型正在分析…" : streaming ? "模型分析完成" : "查看模型思考"}</summary>
            <MarkdownContent content={thought} compact />
            {streaming && thinking && <span className="stream-cursor" />}
          </details>
        )}
        {answer && <MarkdownContent content={answer} />}
        {streaming && answer && <span className="stream-cursor" />}
        {runProgress && <RunActivity progress={runProgress} />}
        {!answer && toolCalls.length > 0 && (
          <p className="tool-intent">正在准备工具操作…</p>
        )}
        {toolCalls.map((call) => (
          <div className="tool-call-chip" key={call.id}>
            {call.name === "render_html" ? <BrowserIcon /> : <FileIcon />}
            <span>{call.name}</span>
            {typeof call.args.path === "string" && <code>{call.args.path}</code>}
            {typeof call.args.title === "string" && <code>{call.args.title}</code>}
          </div>
        ))}
      </div>
      {human && <div className="avatar human-avatar">你</div>}
    </div>
  );
}

function HtmlArtifactCard({
  threadId,
  reference,
  failed,
}: {
  threadId: string;
  reference: ArtifactRef;
  failed: boolean;
}) {
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [mode, setMode] = useState<"preview" | "source">("preview");
  const [renderKey, setRenderKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setArtifact(null);
    setLoadError(null);
    api.artifact(threadId, reference.artifact_id)
      .then((result) => {
        if (!cancelled) setArtifact(result);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setLoadError(errorMessage(reason));
      });
    return () => {
      cancelled = true;
    };
  }, [reference.artifact_id, threadId]);

  return (
    <article className={`artifact-card ${failed ? "error" : ""}`}>
      <header className="artifact-header">
        <div className="artifact-mark"><BrowserIcon /></div>
        <div className="artifact-title">
          <span>RUNNABLE HTML ARTIFACT</span>
          <h3>{reference.title}</h3>
        </div>
        <div className="artifact-actions" role="group" aria-label="预览显示方式">
          <button
            className={mode === "preview" ? "active" : ""}
            onClick={() => setMode("preview")}
            type="button"
          >
            <BrowserIcon /> 预览
          </button>
          <button
            className={mode === "source" ? "active" : ""}
            onClick={() => setMode("source")}
            type="button"
          >
            <CodeIcon /> 源码
          </button>
          <button
            onClick={() => setRenderKey((value) => value + 1)}
            disabled={!artifact || mode !== "preview"}
            type="button"
            title="重新运行页面"
          >
            <RefreshIcon />
            <span className="visually-hidden">重新运行页面</span>
          </button>
        </div>
      </header>

      <div className="artifact-stage">
        {!artifact && !loadError && (
          <div className="artifact-loading">
            <span /><span /><span />
            <p>正在载入持久化页面…</p>
          </div>
        )}
        {loadError && (
          <div className="artifact-load-error" role="alert">
            <strong>页面载入失败</strong>
            <span>{loadError}</span>
          </div>
        )}
        {artifact && mode === "preview" && (
          <iframe
            key={`${artifact.artifact_id}-${renderKey}`}
            title={`${artifact.title} HTML 预览`}
            sandbox="allow-scripts"
            referrerPolicy="no-referrer"
            srcDoc={sandboxDocument(artifact.content)}
          />
        )}
        {artifact && mode === "source" && (
          <pre className="artifact-source"><code>{artifact.content}</code></pre>
        )}
      </div>

      <footer className="artifact-footer">
        <span><i /> 沙箱运行 · 禁止网络与宿主访问</span>
        <code>{formatBytes(reference.byte_size)}</code>
      </footer>
    </article>
  );
}

interface ApprovalCardProps {
  payload: ApprovalPayload;
  busy: string | null;
  onDecision: (approved: boolean) => void;
}

function ApprovalCard({ payload, busy, onDecision }: ApprovalCardProps) {
  const previewing = payload.tool === "render_html";
  const writing = payload.tool === "write_file";
  const deleting = payload.tool === "delete_file";
  const title = previewing
    ? "运行这个 HTML 实时预览吗？"
    : writing
      ? "允许写入这个文件？"
      : "允许删除这个文件？";
  return (
    <section className="approval-wrap" aria-label={previewing ? "实时预览确认" : "人工审批"}>
      <div className={`approval-card ${previewing ? "preview-approval" : ""}`}>
        <div className="approval-icon">{previewing ? <BrowserIcon /> : <ShieldIcon />}</div>
        <div className="approval-main">
          <div className="approval-heading">
            <div>
              <span>{previewing ? "INTERACTIVE PREVIEW" : "HUMAN APPROVAL REQUIRED"}</span>
              <h3>{title}</h3>
            </div>
            <span className={`operation-badge ${previewing ? "preview" : writing ? "write" : "delete"}`}>
              {previewing ? "PREVIEW" : writing ? "WRITE" : "DELETE"}
            </span>
          </div>
          {previewing ? (
            <div className="approval-path preview-title">
              <BrowserIcon />
              <strong>{payload.title || "未命名页面"}</strong>
              <small>{formatBytes(payload.byte_size ?? 0)}</small>
            </div>
          ) : (
            <div className="approval-path"><FileIcon /><code>{payload.path}</code></div>
          )}
          {previewing && (
            <div className="approval-preview html-preview-summary">
              <div>
                <span>即将运行的 HTML</span>
                <small>{payload.characters ?? 0} 字符 · 沙箱隔离</small>
              </div>
              <pre>{payload.preview || "（无可预览内容）"}</pre>
            </div>
          )}
          {writing && (
            <div className="approval-preview">
              <div>
                <span>内容预览</span>
                <small>{payload.characters ?? 0} 字符 · {payload.overwrite ? "覆盖文件" : "新建文件"}</small>
              </div>
              <pre>{payload.preview || "（空文件）"}</pre>
            </div>
          )}
          {deleting && <p className="delete-warning">删除无法由 checkpoint 自动撤销，请确认文件路径。</p>}
          {previewing && <p className="preview-note">确认后才会创建并在隔离沙箱中运行；选择仅查看代码不会创建预览。</p>}
          <div className="approval-actions">
            <button className="reject-button" onClick={() => onDecision(false)} disabled={Boolean(busy)}>
              {busy === "reject" ? "正在处理…" : previewing ? "仅查看代码" : "拒绝"}
            </button>
            <button className="approve-button" onClick={() => onDecision(true)} disabled={Boolean(busy)}>
              {previewing ? <BrowserIcon /> : <ShieldIcon />}
              {busy === "approve" ? "正在执行…" : previewing ? "运行实时预览" : "批准并执行"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

interface ComposerProps {
  value: string;
  disabled: boolean;
  waitingApproval: boolean;
  onChange: (value: string) => void;
  onSubmit: (event: FormEvent) => void;
}

function Composer({ value, disabled, waitingApproval, onChange, onSubmit }: ComposerProps) {
  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      event.currentTarget.form?.requestSubmit();
    }
  };
  return (
    <form className="composer-wrap" onSubmit={onSubmit}>
      <div className={`composer ${disabled ? "disabled" : ""}`}>
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          rows={1}
          placeholder={waitingApproval ? "请先处理上方的操作确认" : "发送消息、操作文件，或生成可预览页面…"}
          aria-label="消息内容"
        />
        <button type="submit" disabled={disabled || !value.trim()} aria-label="发送消息">
          <SendIcon />
        </button>
      </div>
      <div className="composer-hint">
        <span><ShieldIcon /> 文件变更与实时预览需要你的确认</span>
        <span>Enter 发送 · Shift + Enter 换行</span>
      </div>
    </form>
  );
}

interface CheckpointPanelProps {
  checkpoints: Checkpoint[];
  currentId: string | null;
  open: boolean;
  onClose: () => void;
  onFork: (checkpoint: Checkpoint) => void;
}

function CheckpointPanel({ checkpoints, currentId, open, onClose, onFork }: CheckpointPanelProps) {
  return (
    <aside className={`checkpoint-panel ${open ? "mobile-open" : ""}`}>
      <div className="checkpoint-header">
        <div>
          <span>STATE HISTORY</span>
          <h2>Checkpoints</h2>
        </div>
        <button className="icon-button mobile-only" onClick={onClose} aria-label="关闭时间线">
          <CloseIcon />
        </button>
      </div>
      <p className="checkpoint-intro">选择任意状态，在不影响原会话的情况下探索另一条路径。</p>
      <div className="checkpoint-list">
        {!checkpoints.length && (
          <div className="no-checkpoints"><ClockIcon /><span>发送消息后，这里会出现状态历史。</span></div>
        )}
        {checkpoints.map((checkpoint) => {
          const current = checkpoint.checkpoint_id === currentId;
          return (
            <article className={`checkpoint-item ${current ? "current" : ""}`} key={checkpoint.checkpoint_id}>
              <div className="timeline-rail"><i /><span /></div>
              <div className="checkpoint-card">
                <div className="checkpoint-meta">
                  <span>{current ? "CURRENT" : `CP · ${checkpoint.index}`}</span>
                  <time>{formatTime(checkpoint.created_at)}</time>
                </div>
                <p>{checkpointSummary(checkpoint)}</p>
                <div className="checkpoint-stats">
                  <span>{checkpoint.message_count} messages</span>
                  {checkpoint.has_interrupt && <em>等待审批</em>}
                  {checkpoint.next.length > 0 && !checkpoint.has_interrupt && <em>可恢复</em>}
                </div>
                <button onClick={() => onFork(checkpoint)}>
                  <BranchIcon /> 从这里创建分支
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </aside>
  );
}

interface ForkDialogProps {
  checkpoint: Checkpoint;
  name: string;
  busy: boolean;
  onNameChange: (name: string) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}

function ForkDialog({ checkpoint, name, busy, onNameChange, onClose, onSubmit }: ForkDialogProps) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={onClose}>
      <form className="fork-dialog" onSubmit={onSubmit} onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-icon"><BranchIcon /></div>
        <button type="button" className="dialog-close" onClick={onClose} aria-label="关闭">
          <CloseIcon />
        </button>
        <span className="dialog-kicker">FORK CHECKPOINT</span>
        <h2>开启一条新的会话路径</h2>
        <p>新会话会继承此 checkpoint 的记忆，之后的消息和文件审批相互独立。</p>
        <div className="fork-source">
          <small>源 checkpoint</small>
          <code>{checkpoint.checkpoint_id}</code>
          <span>{checkpoint.message_count} 条消息 · {formatTime(checkpoint.created_at)}</span>
        </div>
        <label>
          新会话 ID
          <input
            value={name}
            onChange={(event) => onNameChange(event.target.value)}
            pattern="[A-Za-z0-9][A-Za-z0-9._-]*"
            maxLength={64}
            required
            autoFocus
          />
        </label>
        <div className="dialog-actions">
          <button type="button" onClick={onClose}>取消</button>
          <button type="submit" disabled={busy}>
            <BranchIcon /> {busy ? "正在创建…" : "创建并切换"}
          </button>
        </div>
      </form>
    </div>
  );
}

function LoadingState() {
  return (
    <div className="loading-state">
      <div className="loading-orbit"><span /><span /></div>
      <strong>正在恢复持久化会话</strong>
      <p>从 SQLite 读取最新 checkpoint…</p>
    </div>
  );
}

function RunActivity({ progress }: { progress: RunProgressView }) {
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const elapsedMs = Math.max(progress.elapsed_ms, now - progress.startedAt);
  const heartbeatAge = now - progress.lastEventAt;
  const connectionStale = progress.connected && heartbeatAge > 10000;
  const connectionText = !progress.connected
    ? "正在建立 SSE 流式连接"
    : connectionStale
      ? "超过 10 秒未收到后端心跳，连接可能异常"
    : heartbeatAge <= 3500
      ? "流式连接正常，后端服务正在运行"
      : "等待后端下一次状态更新";

  return (
    <div className={`run-activity ${connectionStale ? "stale" : ""}`} role="status" aria-live="polite">
      <div className="run-activity-heading">
        <span className={`run-live-dot ${progress.connected ? "connected" : ""} ${connectionStale ? "stale" : ""}`} />
        <div>
          <small>{connectionStale ? "CONNECTION DELAYED" : progress.connected ? "BACKEND LIVE" : "CONNECTING"}</small>
          <strong>{progress.message}</strong>
        </div>
        <time>{formatElapsed(elapsedMs)}</time>
      </div>
      <div className="run-activity-meta">
        <span>{connectionText}</span>
        <span>请保持页面开启</span>
      </div>
      <div className="run-activity-track"><i /></div>
    </div>
  );
}

function contentText(content: MessageContent): string {
  return typeof content === "string" ? content : JSON.stringify(content, null, 2);
}

function MarkdownContent({ content, compact = false }: { content: string; compact?: boolean }) {
  return (
    <div className={`markdown-body ${compact ? "compact" : ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}

function splitThought(content: string): { thought: string; answer: string; thinking: boolean } {
  const match = content.match(/<think>([\s\S]*?)<\/think>/i);
  const openThought = !match ? content.match(/^<think>([\s\S]*)$/i) : null;
  return {
    thought: (match?.[1] ?? openThought?.[1] ?? "").trim(),
    answer: openThought
      ? ""
      : content.replace(/<think>[\s\S]*?<\/think>/gi, "").trim(),
    thinking: Boolean(openThought),
  };
}

function isStreamingMessage(message: ChatMessage): boolean {
  return typeof message.id === "string" && message.id.startsWith("streaming-");
}

function createTokenSink(
  setChat: Dispatch<SetStateAction<SessionState | null>>,
): (token: string) => void {
  const messageId = `streaming-${Date.now()}`;
  let started = false;
  return (token: string) => {
    const firstToken = !started;
    started = true;
    setChat((current) => {
      if (!current) return current;
      if (firstToken) {
        return {
          ...current,
          messages: [
            ...current.messages,
            { id: messageId, type: "ai", content: token },
          ],
        };
      }
      return {
        ...current,
        messages: current.messages.map((message) =>
          message.id === messageId
            ? { ...message, content: `${contentText(message.content)}${token}` }
            : message,
        ),
      };
    });
  };
}

function createArtifactSink(
  setChat: Dispatch<SetStateAction<SessionState | null>>,
): (artifact: ArtifactRef) => void {
  return (artifact) => {
    setChat((current) => {
      if (
        !current ||
        current.messages.some(
          (message) => message.artifact?.artifact_id === artifact.artifact_id,
        )
      ) {
        return current;
      }
      return {
        ...current,
        messages: [
          ...current.messages,
          {
            id: `streaming-artifact-${artifact.artifact_id}`,
            type: "tool",
            name: "render_html",
            tool_status: "success",
            content: `已创建可预览页面：${artifact.title}`,
            artifact,
          },
        ],
      };
    });
  };
}

function createStreamHandlers(
  setChat: Dispatch<SetStateAction<SessionState | null>>,
  setRunProgress: Dispatch<SetStateAction<RunProgressView | null>>,
) {
  return {
    onToken: createTokenSink(setChat),
    onArtifact: createArtifactSink(setChat),
    onProgress: createProgressSink(setRunProgress),
  };
}

function beginRunProgress(message: string): RunProgressView {
  const now = Date.now();
  return {
    phase: "connecting",
    message,
    elapsed_ms: 0,
    heartbeat: false,
    startedAt: now,
    lastEventAt: now,
    connected: false,
  };
}

function createProgressSink(
  setRunProgress: Dispatch<SetStateAction<RunProgressView | null>>,
): (progress: StreamProgress) => void {
  return (progress) => {
    const now = Date.now();
    setRunProgress((current) => ({
      ...progress,
      startedAt: current?.startedAt ?? now - progress.elapsed_ms,
      lastEventAt: now,
      connected: true,
    }));
  };
}

function formatElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  if (totalSeconds < 60) return `${totalSeconds} 秒`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes} 分 ${seconds.toString().padStart(2, "0")} 秒`;
}

const PREVIEW_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  "img-src data: blob:",
  "connect-src 'none'",
  "font-src data:",
  "media-src data: blob:",
  "object-src 'none'",
  "base-uri 'none'",
  "form-action 'none'",
].join("; ");

function sandboxDocument(html: string): string {
  const documentNode = new DOMParser().parseFromString(html, "text/html");
  const csp = documentNode.createElement("meta");
  csp.httpEquiv = "Content-Security-Policy";
  csp.content = PREVIEW_CSP;
  documentNode.head.prepend(csp);
  return `<!doctype html>\n${documentNode.documentElement.outerHTML}`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(value < 10 * 1024 ? 1 : 0)} KiB`;
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : "发生未知错误";
}

function statusLabel(status?: SessionState["status"]): string {
  if (status === "waiting_approval") return "等待审批";
  if (status === "recoverable") return "等待恢复";
  return "已同步";
}

function sessionPreview(session: SessionSummary): string {
  if (!session.last_message) return "空白会话";
  const text = splitThought(contentText(session.last_message.content)).answer;
  if (text) return truncate(stripMarkdown(text), 30);
  if (session.last_message.tool_calls?.length) {
    return `工具：${session.last_message.tool_calls[0].name}`;
  }
  return "状态已更新";
}

function checkpointSummary(checkpoint: Checkpoint): string {
  if (!checkpoint.last_message) return "会话初始化";
  const text = splitThought(contentText(checkpoint.last_message.content)).answer;
  if (text) return truncate(stripMarkdown(text).replace(/\n/g, " "), 66);
  const tool = checkpoint.last_message.tool_calls?.[0]?.name;
  return tool ? `准备调用 ${tool}` : `完成 ${checkpoint.source ?? "state"} 状态写入`;
}

function truncate(value: string, length: number): string {
  return value.length > length ? `${value.slice(0, length)}…` : value;
}

function stripMarkdown(value: string): string {
  return value.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/`([^`]+)`/g, "$1");
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function shortDate(value: string): string {
  const date = new Date(value);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit" }).format(date);
  }
  return new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric" }).format(date);
}
