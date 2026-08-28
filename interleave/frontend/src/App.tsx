import { FormEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { streamChat } from "./stream";
import type {
  AnswerItem,
  RunView,
  StreamEvent,
  ThoughtItem,
  TimelineItem,
  ToolItem,
} from "./types";

const examples = [
  "介绍一下 LangGraph 的流式事件",
  "解释 interleave 的作用",
  "用三点总结这个项目",
];

export default function App() {
  const [input, setInput] = useState("");
  const [run, setRun] = useState<RunView | null>(null);
  const [events, setEvents] = useState<StreamEvent[]>([]);
  const [now, setNow] = useState(Date.now() / 1000);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const active = run?.status === "connecting" || run?.status === "running";

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 250);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [run]);

  useEffect(() => () => abortRef.current?.abort(), []);

  async function submit(message: string) {
    const prompt = message.trim();
    if (!prompt || active) return;

    const controller = new AbortController();
    abortRef.current = controller;
    setEvents([]);
    setRun(createPendingRun(prompt));
    setInput("");

    try {
      await streamChat(
        prompt,
        (event) => {
          setEvents((current) => [...current.slice(-199), event]);
          setRun((current) => reduceEvent(current, event));
        },
        controller.signal,
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      const message = error instanceof Error ? error.message : "Unknown stream error";
      setRun((current) =>
        current ? { ...current, status: "failed", error: message } : current,
      );
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }

  function stop() {
    abortRef.current?.abort();
    setRun((current) =>
      current ? { ...current, status: "interrupted" } : current,
    );
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submit(input);
  }

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <LogoIcon />
          <div>
            <strong>Event Timeline</strong>
            <span>LangGraph protocol v3</span>
          </div>
        </div>

        <div className="topbar-actions">
          <span className={`connection-badge ${active ? "active" : ""}`}>
            <i />
            {active ? "Streaming" : "Ready"}
          </span>
          <button
            className={`inspector-toggle ${inspectorOpen ? "selected" : ""}`}
            type="button"
            onClick={() => setInspectorOpen((value) => !value)}
          >
            <BracesIcon />
            Events
            {events.length > 0 && <em>{events.length}</em>}
          </button>
        </div>
      </header>

      <div className={`workspace ${inspectorOpen ? "with-inspector" : ""}`}>
        <main className="conversation">
          <div className="conversation-scroll">
            <div className="conversation-inner">
              {run ? (
                <RunTimeline run={run} now={now} />
              ) : (
                <EmptyState onSelect={(value) => setInput(value)} />
              )}
              <div ref={bottomRef} />
            </div>
          </div>

          <form className="composer" onSubmit={onSubmit}>
            <div className="composer-box">
              <textarea
                aria-label="Message"
                placeholder="让 graph 开始一项任务…"
                rows={1}
                value={input}
                disabled={active}
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
              />
              {active ? (
                <button className="send-button stop" type="button" onClick={stop}>
                  <StopIcon />
                  <span className="sr-only">Stop</span>
                </button>
              ) : (
                <button
                  className="send-button"
                  type="submit"
                  disabled={!input.trim()}
                >
                  <SendIcon />
                  <span className="sr-only">Send</span>
                </button>
              )}
            </div>
            <p>Enter 发送 · Shift + Enter 换行 · 事件按服务端 sequence 排序</p>
          </form>
        </main>

        {inspectorOpen && <EventInspector events={events} />}
      </div>
    </div>
  );
}

function EmptyState({ onSelect }: { onSelect: (value: string) => void }) {
  return (
    <section className="empty-state">
      <div className="empty-orbit">
        <span />
        <LogoIcon />
      </div>
      <p className="eyebrow">LIVE GRAPH OBSERVABILITY</p>
      <h1>看见 graph 的每一步。</h1>
      <p className="empty-copy">
        Reasoning、工具调用、token 和状态快照会沿同一条时间线实时抵达。
      </p>
      <div className="example-grid">
        {examples.map((example) => (
          <button key={example} type="button" onClick={() => onSelect(example)}>
            <SparkIcon />
            {example}
          </button>
        ))}
      </div>
    </section>
  );
}

function RunTimeline({ run, now }: { run: RunView; now: number }) {
  return (
    <section className="run-view">
      <div className="run-overview">
        <div>
          <span>RUN</span>
          <code>{run.id.replace("run_", "").slice(0, 10)}</code>
        </div>
        <div>
          <span>STATE</span>
          <strong>{statusLabel(run.status)}</strong>
        </div>
        <div>
          <span>SNAPSHOTS</span>
          <strong>{run.stateUpdates}</strong>
        </div>
      </div>

      <div className="timeline">
        <TimelineNode tone="user">
          <div className="prompt-card">
            <span>You</span>
            <p>{run.prompt}</p>
          </div>
        </TimelineNode>

        {run.items.map((item) => (
          <TimelineItemView key={item.id} item={item} now={now} />
        ))}

        {(run.status === "connecting" || run.status === "running") && (
          <TimelineNode tone="live">
            <div className="live-step">
              <span className="typing-dots"><i /><i /><i /></span>
              Waiting for the next event
            </div>
          </TimelineNode>
        )}

        {run.status === "completed" && (
          <TimelineNode tone="success">
            <div className="terminal-step">
              <CheckIcon />
              Completed in {formatDuration((run.durationMs ?? 0) / 1000)}
            </div>
          </TimelineNode>
        )}

        {run.status === "interrupted" && (
          <TimelineNode tone="warning">
            <div className="terminal-step">Run interrupted</div>
          </TimelineNode>
        )}

        {run.status === "failed" && (
          <TimelineNode tone="error">
            <div className="error-card">
              <AlertIcon />
              <div><strong>Run failed</strong><p>{run.error}</p></div>
            </div>
          </TimelineNode>
        )}
      </div>
    </section>
  );
}

function TimelineItemView({ item, now }: { item: TimelineItem; now: number }) {
  if (item.kind === "thought") {
    const duration = (item.completedAt ?? now) - item.startedAt;
    return (
      <TimelineNode tone="thought">
        <ThoughtBlock item={item} duration={duration} />
      </TimelineNode>
    );
  }

  if (item.kind === "tool") {
    return (
      <TimelineNode tone={item.status === "failed" ? "error" : "tool"}>
        <ToolBlock item={item} />
      </TimelineNode>
    );
  }

  return (
    <TimelineNode tone="answer">
      <div className="answer-block markdown">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>
          {item.content || " "}
        </ReactMarkdown>
        {!item.completedAt && <span className="stream-caret" />}
      </div>
    </TimelineNode>
  );
}

function TimelineNode({
  tone,
  children,
}: {
  tone: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`timeline-node tone-${tone}`}>
      <span className="node-dot" />
      <div className="node-content">{children}</div>
    </div>
  );
}

function ThoughtBlock({ item, duration }: { item: ThoughtItem; duration: number }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="thought-block">
      <button type="button" onClick={() => setOpen((value) => !value)}>
        <span>Thought for {formatDuration(duration)}</span>
        <ChevronIcon open={open} />
      </button>
      {open && (
        <div className="thought-copy markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {item.content || (item.completedAt
              ? "模型完成了内部推理，未返回可展示的摘要。"
              : "正在思考…")}
          </ReactMarkdown>
          {!item.completedAt && <span className="stream-caret" />}
        </div>
      )}
    </div>
  );
}

function ToolBlock({ item }: { item: ToolItem }) {
  return (
    <div className={`tool-block status-${item.status}`}>
      <div className="tool-heading">
        <ToolIcon />
        <strong>{item.name || "Tool"}</strong>
        {item.input && <code>{compactValue(item.input)}</code>}
        <span>{item.status}</span>
      </div>
      {item.output && <pre>{item.output}</pre>}
    </div>
  );
}

function EventInspector({ events }: { events: StreamEvent[] }) {
  return (
    <aside className="event-inspector">
      <div className="inspector-header">
        <div>
          <span>PROTOCOL LOG</span>
          <strong>Live events</strong>
        </div>
        <em>{events.length}</em>
      </div>
      <div className="event-list">
        {events.length === 0 ? (
          <div className="inspector-empty">
            <BracesIcon />
            <p>事件将在这里逐条出现</p>
          </div>
        ) : (
          events.map((event, index) => (
            <details className="event-row" key={`${event.sequence ?? index}-${event.type}`}>
              <summary>
                <span>{String(event.sequence ?? index + 1).padStart(2, "0")}</span>
                <strong>{event.type}</strong>
                <time>{formatClock(event.timestamp)}</time>
              </summary>
              <pre>{JSON.stringify(event, null, 2)}</pre>
            </details>
          ))
        )}
      </div>
    </aside>
  );
}

function reduceEvent(current: RunView | null, event: StreamEvent): RunView {
  const timestamp = numberValue(event.timestamp, Date.now() / 1000);
  const sequence = numberValue(event.sequence, 0);

  if (event.type === "run.started") {
    const input = recordValue(event.input);
    return {
      id: stringValue(event.run_id, "pending"),
      prompt: stringValue(input.content, current?.prompt ?? ""),
      status: "running",
      startedAt: timestamp,
      items: [],
      stateUpdates: 0,
    };
  }

  const run = current ?? createPendingRun("");
  const blockIndex = numberValue(event.block_index, 0);
  const messageId = stringValue(event.message_id, "message");

  if (event.type === "content.started") {
    const content = recordValue(event.content);
    const contentType = stringValue(content.type, "");
    if (contentType === "reasoning") {
      return upsertItem(run, `thought:${messageId}:${blockIndex}`, () => ({
        kind: "thought",
        id: `thought:${messageId}:${blockIndex}`,
        sequence,
        startedAt: timestamp,
        content: "",
      }));
    }
    if (contentType === "text") {
      return upsertItem(run, `answer:${messageId}:${blockIndex}`, () => ({
        kind: "answer",
        id: `answer:${messageId}:${blockIndex}`,
        sequence,
        startedAt: timestamp,
        content: "",
      }));
    }
  }

  if (event.type === "reasoning.delta") {
    const id = `thought:${messageId}:${blockIndex}`;
    return mutateItem(
      upsertItem(run, id, () => ({
        kind: "thought",
        id,
        sequence,
        startedAt: timestamp,
        content: "",
      })),
      id,
      (item) => item.kind === "thought"
        ? { ...item, content: item.content + stringValue(event.text, "") }
        : item,
    );
  }

  if (event.type === "text.delta") {
    const id = `answer:${messageId}:${blockIndex}`;
    return mutateItem(
      upsertItem(run, id, () => ({
        kind: "answer",
        id,
        sequence,
        startedAt: timestamp,
        content: "",
      })),
      id,
      (item) => item.kind === "answer"
        ? { ...item, content: item.content + stringValue(event.text, "") }
        : item,
    );
  }

  if (event.type === "tool_call.delta") {
    const delta = recordValue(event.delta);
    const fields = recordValue(delta.fields);
    const id = `tool-call:${messageId}:${blockIndex}`;
    return mutateItem(
      upsertItem(run, id, () => ({
        kind: "tool",
        id,
        sequence,
        startedAt: timestamp,
        name: stringValue(fields.name, "Tool call"),
        status: "preparing",
        input: "",
        output: "",
      })),
      id,
      (item) => item.kind === "tool"
        ? {
            ...item,
            name: stringValue(fields.name, item.name),
            input: stringValue(fields.args, item.input),
          }
        : item,
    );
  }

  if (event.type.startsWith("tool.")) {
    const tool = recordValue(event.tool);
    const name = stringValue(tool.tool_name, stringValue(tool.name, "Tool"));
    const callId = stringValue(
      tool.tool_call_id,
      stringValue(tool.id, `${name}:${sequence}`),
    );
    const id = `tool:${callId}`;
    const status: ToolItem["status"] = event.type === "tool.started"
      ? "running"
      : event.type === "tool.failed"
        ? "failed"
        : event.type === "tool.completed"
          ? "completed"
          : "running";
    return mutateItem(
      upsertItem(run, id, () => ({
        kind: "tool",
        id,
        sequence,
        startedAt: timestamp,
        name,
        status,
        input: displayValue(tool.input),
        output: "",
      })),
      id,
      (item) => item.kind === "tool"
        ? {
            ...item,
            status,
            output: event.type === "tool.output.delta"
              ? item.output + displayValue(tool.output ?? tool.data)
              : displayValue(tool.output ?? item.output),
            completedAt: status === "completed" || status === "failed"
              ? timestamp
              : item.completedAt,
          }
        : item,
    );
  }

  if (event.type === "content.completed") {
    const content = recordValue(event.content);
    const prefix = stringValue(content.type, "") === "reasoning" ? "thought" : "answer";
    return mutateItem(run, `${prefix}:${messageId}:${blockIndex}`, (item) => ({
      ...item,
      completedAt: timestamp,
    }));
  }

  if (event.type === "state.updated") {
    return { ...run, stateUpdates: run.stateUpdates + 1 };
  }

  if (event.type === "run.completed") {
    return {
      ...run,
      status: "completed",
      durationMs: numberValue(event.duration_ms, (timestamp - run.startedAt) * 1000),
    };
  }

  if (event.type === "run.interrupted") {
    return { ...run, status: "interrupted" };
  }

  if (event.type === "run.failed") {
    const error = recordValue(event.error);
    return {
      ...run,
      status: "failed",
      error: stringValue(error.message, "The graph run failed."),
      durationMs: numberValue(event.duration_ms, (timestamp - run.startedAt) * 1000),
    };
  }

  return run;
}

function createPendingRun(prompt: string): RunView {
  return {
    id: "connecting",
    prompt,
    status: "connecting",
    startedAt: Date.now() / 1000,
    items: [],
    stateUpdates: 0,
  };
}

function upsertItem(
  run: RunView,
  id: string,
  create: () => TimelineItem,
): RunView {
  if (run.items.some((item) => item.id === id)) return run;
  return { ...run, items: [...run.items, create()] };
}

function mutateItem(
  run: RunView,
  id: string,
  mutate: (item: TimelineItem) => TimelineItem,
): RunView {
  return {
    ...run,
    items: run.items.map((item) => item.id === id ? mutate(item) : item),
  };
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stringValue(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function numberValue(value: unknown, fallback: number): number {
  return typeof value === "number" ? value : fallback;
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function compactValue(value: string): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > 72 ? `${compact.slice(0, 69)}…` : compact;
}

function formatDuration(seconds: number): string {
  if (seconds < 1) return `${Math.max(0, seconds).toFixed(1)}s`;
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}

function formatClock(value: unknown): string {
  if (typeof value !== "number") return "--:--:--";
  return new Date(value * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}

function statusLabel(status: RunView["status"]): string {
  return {
    connecting: "CONNECTING",
    running: "STREAMING",
    completed: "COMPLETED",
    interrupted: "INTERRUPTED",
    failed: "FAILED",
  }[status];
}

function LogoIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4v16M6 8h6a5 5 0 0 1 5 5v7M6 15h4" /><circle cx="6" cy="4" r="2" /><circle cx="17" cy="20" r="2" /><circle cx="10" cy="15" r="2" /></svg>;
}

function SendIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 14-7-5 14-2.8-5.2L5 12Z" /><path d="m11.2 13.8 3.1-3.1" /></svg>;
}

function StopIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" /></svg>;
}

function SparkIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 1.2 4.2L17 9l-3.8 1.8L12 15l-1.2-4.2L7 9l3.8-1.8L12 3Z" /><path d="m18 15 .7 2.3L21 18l-2.3.7L18 21l-.7-2.3L15 18l2.3-.7L18 15Z" /></svg>;
}

function ToolIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 9 4 12l4 3M16 9l4 3-4 3M14 6l-4 12" /></svg>;
}

function CheckIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" /></svg>;
}

function AlertIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4 3 20h18L12 4Z" /><path d="M12 9v5M12 17.5v.1" /></svg>;
}

function ChevronIcon({ open }: { open: boolean }) {
  return <svg className={open ? "open" : ""} viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>;
}

function BracesIcon() {
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 4H7a2 2 0 0 0-2 2v3a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5a2 2 0 0 0 2 2h2M15 4h2a2 2 0 0 1 2 2v3a2 2 0 0 0 2 2 2 2 0 0 0-2 2v5a2 2 0 0 1-2 2h-2" /></svg>;
}
