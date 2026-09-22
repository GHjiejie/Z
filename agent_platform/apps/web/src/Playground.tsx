import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import {
  AlertCircle,
  ArrowUp,
  Bot,
  Check,
  ChevronDown,
  CircleStop,
  LoaderCircle,
  MessageSquare,
  Plus,
  RefreshCw,
  Sparkles,
  Terminal,
  UserRound,
  Wrench,
} from "lucide-react";
import { API_ROOT, api, dateTime, terminal, write } from "./api";
import { Badge, Button, PageTitle, useResource, useToast } from "./components";
import type { Agent, Run, RunEvent, Session, SessionDetail } from "./types";

const query = () => new URLSearchParams(location.hash.split("?")[1] ?? "");
const eventNames = [
  "run.started",
  "run.queued",
  "run.completed",
  "run.failed",
  "run.cancelled",
  "run.canceled",
  "run.interrupted",
  "run.expired",
  "run.succeeded",
  "message.delta",
  "tool.started",
  "tool.finished",
  "usage.updated",
  "step.started",
  "step.completed",
  "step.finished",
  "model.started",
  "model.completed",
];
export function Playground() {
  const agents = useResource<{ items: Agent[] }>("/agents");
  const sessions = useResource<{ items: Session[] }>("/sessions");
  const [agentId, setAgentId] = useState(query().get("agent") ?? "");
  const [sessionId, setSessionId] = useState<string | null>(
    query().get("session"),
  );
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [draft, setDraft] = useState("");
  const [liveText, setLiveText] = useState("");
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [error, setError] = useState("");
  const [connection, setConnection] = useState<
    "closed" | "connecting" | "open" | "retrying"
  >("closed");
  const [reconnect, setReconnect] = useState(0);
  const [showEvents, setShowEvents] = useState(false);
  const lastSequence = useRef(0);
  const streamRun = useRef("");
  const bottom = useRef<HTMLDivElement>(null);
  const sendKey = useRef<{
    message: string;
    session: string;
    key: string;
  } | null>(null);
  const selection = useRef(0);
  const toast = useToast();
  const published =
    agents.data?.items.filter((agent) => agent.published_version) ?? [];
  const selectedAgent = agents.data?.items.find(
    (agent) => agent.id === (detail?.agent_id ?? agentId),
  );
  const latestRun = detail?.runs
    .slice()
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];
  useEffect(() => {
    if (!agentId && published.length) setAgentId(published[0].id);
  }, [agentId, published]);
  useEffect(() => {
    const listener = () => {
      const next = query();
      if (next.has("session")) setSessionId(next.get("session"));
      if (next.has("agent")) {
        setAgentId(next.get("agent")!);
        setSessionId(null);
      }
    };
    window.addEventListener("hashchange", listener);
    return () => window.removeEventListener("hashchange", listener);
  }, []);
  const loadSession = useCallback(async (id: string, updateRun = true) => {
    const current = ++selection.current;
    setLoading(true);
    setError("");
    try {
      const result = await api<SessionDetail>(`/sessions/${id}`);
      if (current !== selection.current) return;
      setDetail(result);
      setAgentId(result.agent_id);
      if (updateRun)
        setActiveRun(
          [...result.runs].reverse().find((run) => !terminal(run.status)) ??
            null,
        );
    } catch (err) {
      if (current === selection.current) setError((err as Error).message);
    } finally {
      if (current === selection.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    setActiveRun(null);
    setLiveText("");
    setEvents([]);
    setDetail(null);
    setError("");
    if (sessionId) void loadSession(sessionId);
    else {
      selection.current++;
      setLoading(false);
    }
    return () => {
      selection.current++;
    };
  }, [sessionId, loadSession]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [detail?.messages.length, liveText, events.length]);
  useEffect(() => {
    if (!activeRun || terminal(activeRun.status)) {
      setConnection("closed");
      return;
    }
    const runId = activeRun.id;
    const runSession = activeRun.session_id;
    if (streamRun.current !== runId) {
      streamRun.current = runId;
      lastSequence.current = 0;
      setLiveText("");
      setEvents([]);
    }
    let disposed = false;
    let finishing = false;
    let pollBusy = false;
    setConnection("connecting");
    const source = new EventSource(
      `${API_ROOT}/runs/${runId}/events?after=${lastSequence.current}`,
      { withCredentials: true },
    );
    const finish = async (status: string, message?: string) => {
      if (disposed || finishing) return;
      finishing = true;
      source.close();
      setConnection("closed");
      try {
        const result = await api<SessionDetail>(`/sessions/${runSession}`);
        if (!disposed) {
          setDetail(result);
          setLiveText("");
        }
      } catch (err) {
        if (!disposed) setError((err as Error).message);
      }
      if (!disposed) {
        setActiveRun(null);
        setCanceling(false);
        if (status === "failed" && message) setError(message);
        void sessions.reload();
      }
    };
    const receive = (message: MessageEvent<string>) => {
      if (disposed) return;
      let event: RunEvent;
      try {
        event = JSON.parse(message.data) as RunEvent;
      } catch {
        return;
      }
      if (!event.type || event.sequence <= lastSequence.current) return;
      lastSequence.current = event.sequence;
      setConnection("open");
      const payload = event.data ?? {};
      if (event.type === "message.delta") {
        const text = payload.text ?? payload.delta ?? payload.content;
        if (typeof text === "string") setLiveText((value) => value + text);
      } else setEvents((previous) => [...previous.slice(-79), event]);
      if (event.type === "run.started")
        setActiveRun((value) =>
          value ? { ...value, status: "running" } : value,
        );
      if (
        [
          "run.completed",
          "run.failed",
          "run.cancelled",
          "run.canceled",
          "run.interrupted",
          "run.expired",
          "run.succeeded",
        ].includes(event.type)
      )
        void finish(
          event.type.slice(4),
          typeof payload.error === "string"
            ? payload.error
            : typeof payload.message === "string"
              ? payload.message
              : undefined,
        );
    };
    source.onmessage = receive;
    eventNames.forEach((name) =>
      source.addEventListener(name, receive as EventListener),
    );
    source.onopen = () => {
      if (!disposed) setConnection("open");
    };
    source.onerror = () => {
      if (!disposed && !finishing) setConnection("retrying");
    };
    const poll = setInterval(() => {
      if (pollBusy || disposed || finishing) return;
      pollBusy = true;
      void api<Run>(`/runs/${runId}`)
        .then((run) => {
          if (!disposed && terminal(run.status))
            void finish(run.status, run.error);
        })
        .catch(() => {
          /* EventSource retries; keep the last visible result. */
        })
        .finally(() => {
          pollBusy = false;
        });
    }, 5000);
    return () => {
      disposed = true;
      source.close();
      clearInterval(poll);
    };
    // Only a different run or explicit reconnect should rebuild the stream.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.id, reconnect]);
  async function send(event: FormEvent) {
    event.preventDefault();
    const message = draft.trim();
    if (!message || activeRun || busy || loading || !agentId) return;
    setBusy(true);
    setError("");
    try {
      let id = sessionId;
      if (!id) {
        const session = await write<Session>("/sessions", {
          agent_id: agentId,
          title: message.slice(0, 60),
        });
        id = session.id;
        setSessionId(id);
        location.hash = `playground?session=${id}`;
        await sessions.reload();
      }
      if (
        !sendKey.current ||
        sendKey.current.message !== message ||
        sendKey.current.session !== id
      )
        sendKey.current = { message, session: id, key: crypto.randomUUID() };
      const run = await api<Run>(`/sessions/${id}/runs`, {
        method: "POST",
        body: JSON.stringify({ message }),
        headers: { "Idempotency-Key": sendKey.current.key },
      });
      setDraft("");
      sendKey.current = null;
      await loadSession(id, false);
      setActiveRun(run);
      setLiveText("");
      setEvents([]);
      setShowEvents(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function cancel() {
    if (!activeRun || canceling) return;
    setCanceling(true);
    try {
      const run = await write<Run>(`/runs/${activeRun.id}/cancel`);
      toast("已请求取消，正在等待运行停止");
      if (terminal(run.status)) {
        await loadSession(run.session_id);
        setLiveText("");
        setCanceling(false);
      }
    } catch (err) {
      setError((err as Error).message);
      setCanceling(false);
    }
  }
  function newSession() {
    setSessionId(null);
    setDetail(null);
    setActiveRun(null);
    setLiveText("");
    setEvents([]);
    setError("");
    setDraft("");
    setCanceling(false);
    location.hash = `playground${agentId ? `?agent=${agentId}` : ""}`;
  }
  function chooseSession(session: Session) {
    setSessionId(session.id);
    setDraft("");
    setCanceling(false);
    location.hash = `playground?session=${session.id}`;
  }
  const effectiveError = error || agents.error;
  return (
    <>
      <PageTitle
        eyebrow="PLAYGROUND"
        title="对话实验室"
        description="与已发布的 Agent 对话，实时观察模型与工具的协作。"
        action={
          <Button variant="secondary" onClick={newSession} disabled={busy}>
            <Plus size={16} />
            新建对话
          </Button>
        }
      />
      <div className="playground-layout">
        <aside className="session-sidebar">
          <div className="session-heading">
            <span>最近会话</span>
            <button
              className="icon-button"
              aria-label="刷新会话"
              onClick={() => void sessions.reload()}
            >
              <RefreshCw size={14} />
            </button>
          </div>
          {sessions.error && (
            <div className="session-error">{sessions.error}</div>
          )}
          {sessions.loading && !sessions.data ? (
            <div className="session-loading">
              <LoaderCircle className="spin" size={18} />
            </div>
          ) : sessions.data?.items.length ? (
            <div className="session-list">
              {sessions.data.items.map((session) => (
                <button
                  key={session.id}
                  className={`session-item ${session.id === sessionId ? "selected" : ""}`}
                  onClick={() => chooseSession(session)}
                  disabled={busy}
                >
                  <MessageSquare size={16} />
                  <span>
                    <strong>{session.title || "新会话"}</strong>
                    <small>{dateTime(session.created_at)}</small>
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div className="session-empty">
              <MessageSquare size={22} />
              <p>你的会话将保存在这里</p>
            </div>
          )}
          <div className="session-foot">
            <ShieldNote />
          </div>
        </aside>
        <section className="chat-panel">
          <div className="mobile-session-picker">
            <MessageSquare size={14} />
            <select
              aria-label="切换最近会话"
              value={sessionId ?? ""}
              disabled={busy}
              onChange={(event) => {
                const session = sessions.data?.items.find(
                  (item) => item.id === event.target.value,
                );
                if (session) chooseSession(session);
                else newSession();
              }}
            >
              <option value="">新对话</option>
              {sessions.data?.items.map((session) => (
                <option key={session.id} value={session.id}>
                  {session.title || "新会话"}
                </option>
              ))}
            </select>
          </div>
          <div className="chat-heading">
            <div className="chat-agent-icon">
              <Bot size={23} />
            </div>
            <div className="chat-agent-select">
              <label htmlFor="active-agent">当前 Agent</label>
              <div>
                <select
                  id="active-agent"
                  value={detail?.agent_id ?? agentId}
                  disabled={Boolean(sessionId) || busy || Boolean(activeRun)}
                  onChange={(event) => setAgentId(event.target.value)}
                >
                  <option value="" disabled>
                    选择已发布的 Agent
                  </option>
                  {published.map((agent) => (
                    <option value={agent.id} key={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
                <ChevronDown size={14} />
              </div>
            </div>
            <div className="chat-heading-right">
              {activeRun ? (
                <Badge status={activeRun.status} />
              ) : selectedAgent?.published_version ? (
                <span className="chat-version">
                  已发布 v{selectedAgent.published_version}
                </span>
              ) : null}
              {activeRun && (
                <span
                  className={`connection ${connection}`}
                  title={
                    connection === "retrying"
                      ? "正在恢复连接，运行仍在服务端继续"
                      : "实时事件连接"
                  }
                >
                  <i />
                  {connection === "open"
                    ? "实时连接"
                    : connection === "retrying"
                      ? "重新连接中"
                      : "连接中"}
                </span>
              )}
            </div>
          </div>
          <div
            className="chat-messages"
            aria-live="polite"
            aria-busy={Boolean(activeRun)}
          >
            {loading && !detail ? (
              <div className="loading">
                <LoaderCircle className="spin" size={22} />
                正在恢复会话…
              </div>
            ) : !detail?.messages.length && !liveText ? (
              <div className="chat-welcome">
                <div className="chat-welcome-icon">
                  <Sparkles size={33} strokeWidth={1.6} />
                </div>
                <span className="eyebrow">A SPACE FOR YOUR IDEAS</span>
                <h2>
                  {selectedAgent
                    ? `与${selectedAgent.name}一起探索`
                    : "从一个好问题开始"}
                </h2>
                <p>
                  {selectedAgent?.description ||
                    "选择已发布的 Agent，开始一段新的对话。"}
                  <br />
                  每次调用的用量和费用都将自动记录。
                </p>
                {published.length === 0 && !agents.loading ? (
                  <div className="inline-note">
                    还没有已发布的 Agent。请先配置模型，创建并发布 Agent。
                  </div>
                ) : (
                  <div className="prompt-suggestions">
                    {[
                      "请介绍你可以帮助我完成哪些工作",
                      "帮我把一个想法拆解成可执行的计划",
                    ].map((prompt) => (
                      <button
                        key={prompt}
                        onClick={() => setDraft(prompt)}
                        disabled={Boolean(activeRun)}
                      >
                        <MessageSquare size={15} />
                        {prompt}
                        <ArrowUp size={14} />
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="message-list">
                {detail?.messages
                  .filter((message) =>
                    ["user", "assistant"].includes(message.role),
                  )
                  .map((message, index) => (
                    <div
                      key={`${sessionId}-${index}`}
                      className={`chat-message ${message.role}`}
                    >
                      <span className={`message-avatar ${message.role}`}>
                        {message.role === "user" ? (
                          <UserRound size={17} />
                        ) : (
                          <Bot size={18} />
                        )}
                      </span>
                      <div>
                        <div className="message-meta">
                          <strong>
                            {message.role === "user"
                              ? "你"
                              : (selectedAgent?.name ?? "Agent")}
                          </strong>
                          {message.created_at && (
                            <time>{dateTime(message.created_at)}</time>
                          )}
                        </div>
                        <div className="message-body">{message.content}</div>
                      </div>
                    </div>
                  ))}
                {activeRun && (
                  <div className="chat-message assistant">
                    <span className="message-avatar assistant">
                      <Bot size={18} />
                    </span>
                    <div>
                      <div className="message-meta">
                        <strong>{selectedAgent?.name ?? "Agent"}</strong>
                        <span className="muted">
                          {activeRun.status === "queued"
                            ? "等待执行"
                            : "正在回复"}
                        </span>
                      </div>
                      <div className="message-body">
                        {liveText || (
                          <span className="thinking-dots">
                            <i />
                            <i />
                            <i />
                          </span>
                        )}
                        {liveText && <span className="typing-cursor" />}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            )}
            {latestRun?.error && !activeRun && (
              <div className="run-error-summary">
                <AlertCircle size={15} />
                <span>{latestRun.error}</span>
              </div>
            )}
            <div ref={bottom} />
          </div>
          {(events.length > 0 || activeRun) && (
            <div className="run-events">
              <button onClick={() => setShowEvents((value) => !value)}>
                <Terminal size={14} />
                <span>运行过程</span>
                <span className="event-count">{events.length}</span>
                <ChevronDown
                  size={14}
                  className={showEvents ? "rotated" : ""}
                />
              </button>
              {showEvents && (
                <div className="event-list">
                  {events.length ? (
                    events.map((event) => (
                      <div className="event-row" key={event.sequence}>
                        {event.type.startsWith("tool.") ? (
                          <Wrench size={13} />
                        ) : event.type === "run.completed" ? (
                          <Check size={13} />
                        ) : (
                          <span className="event-dot" />
                        )}
                        <span>{eventLabel(event)}</span>
                        <small>#{event.sequence}</small>
                      </div>
                    ))
                  ) : (
                    <span className="muted">等待 Worker 开始执行…</span>
                  )}
                </div>
              )}
            </div>
          )}
          {effectiveError && (
            <div className="chat-error" role="alert">
              <AlertCircle size={16} />
              <span>{effectiveError}</span>
              {sessionId && (
                <button onClick={() => void loadSession(sessionId)}>
                  重新加载
                </button>
              )}
            </div>
          )}
          {connection === "retrying" && (
            <div className="chat-reconnect">
              <LoaderCircle size={14} className="spin" />
              <span>连接暂时中断，正在自动恢复；Agent 会继续运行。</span>
              <button onClick={() => setReconnect((value) => value + 1)}>
                立即重连
              </button>
            </div>
          )}
          <form className="chat-composer" onSubmit={send}>
            <div className="composer-input">
              <textarea
                aria-label="发送给 Agent 的消息"
                placeholder={
                  selectedAgent
                    ? `发送消息给${selectedAgent.name}…`
                    : "请先选择一个已发布的 Agent"
                }
                rows={2}
                value={draft}
                disabled={busy || loading || !agentId}
                maxLength={32000}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (
                    event.key === "Enter" &&
                    !event.shiftKey &&
                    !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault();
                    if (!activeRun) void send(event);
                  }
                }}
              />
              <div className="composer-bottom">
                <span>Enter 发送 · Shift + Enter 换行</span>
                {activeRun ? (
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() => void cancel()}
                    busy={canceling}
                  >
                    <CircleStop size={15} />
                    停止生成
                  </Button>
                ) : (
                  <Button
                    type="submit"
                    disabled={!draft.trim() || !agentId || loading}
                    busy={busy}
                    aria-label="发送消息"
                  >
                    <ArrowUp size={18} />
                  </Button>
                )}
              </div>
            </div>
            <div className="composer-note">
              回答由模型生成，请核实关键信息。取消不会自动免除已产生的费用。
            </div>
          </form>
        </section>
      </div>
    </>
  );
}
function ShieldNote() {
  return (
    <>
      <Check size={14} />
      <span>
        会话与运行事件持续保存
        <br />
        关闭页面不会取消运行
      </span>
    </>
  );
}
function eventLabel(event: RunEvent) {
  const data = event.data ?? {};
  const label: Record<string, string> = {
    "run.started": "Agent 开始执行",
    "run.queued": "运行已进入队列",
    "run.completed": "运行完成",
    "run.failed": "运行失败",
    "run.cancelled": "运行已取消",
    "run.interrupted": "运行已中断",
    "run.expired": "运行已超时",
    "run.succeeded": "运行完成",
    "usage.updated": "调用用量与费用已更新",
    "step.started": "开始下一步骤",
    "step.completed": "步骤完成",
    "step.finished": "步骤完成",
    "model.started": "正在调用模型",
    "model.completed": "模型调用完成",
  };
  if (event.type.startsWith("tool."))
    return `${event.type === "tool.started" ? "正在调用" : "工具完成"}：${String(data.name ?? data.tool ?? "工具")}`;
  return label[event.type] ?? event.type;
}
