import { memo, useState } from "react";
import { Brain, ChevronDown, ListChecks, Wrench } from "lucide-react";
import { ChatMarkdown } from "./ChatMarkdown";
import { Spinner } from "./components";
import type { ChatMessage } from "./chatHistory";
import type { ChatRun } from "./chatRuns";
import { replyParts, type WorkflowEntry } from "./workflow";

const statusLabels: Record<WorkflowEntry["status"], string> = {
  pending: "待执行",
  running: "进行中",
  waiting: "等待你处理",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

function ActivityDetails({ entries }: { entries: WorkflowEntry[] }) {
  const [open, setOpen] = useState(false);
  const tools = entries.filter((entry) => entry.kind === "tool");
  const latest = entries.at(-1);
  const focus =
    entries.find((entry) => entry.status === "waiting") ??
    entries.find((entry) => entry.status === "running") ??
    entries.find((entry) => entry.status === "pending") ??
    (latest && ["failed", "cancelled", "interrupted"].includes(latest.status)
      ? latest
      : undefined) ??
    tools.find((entry) => entry.status === "failed") ??
    tools.at(-1) ??
    latest;
  if (!focus) return null;
  const Icon =
    focus.kind === "tool"
      ? Wrench
      : focus.kind === "model"
        ? Brain
        : ListChecks;
  const label =
    focus.kind === "model"
      ? focus.reasoning
        ? "思考摘要"
        : "分析请求"
      : focus.title;
  return (
    <details
      className="reply-activity"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <Icon size={14} aria-hidden="true" />
        <span className="reply-activity-label">
          {label}
          {focus.kind === "tool" && tools.length > 1
            ? ` 等 ${tools.length} 项工具操作`
            : ""}
        </span>
        {focus.status !== "succeeded" && (
          <span className="reply-activity-status">
            · {statusLabels[focus.status]}
          </span>
        )}
        <ChevronDown size={13} className="reply-chevron" aria-hidden="true" />
      </summary>
      {open && (
        <ol className="reply-activity-details" aria-label="操作详情">
          {entries.map((entry) => (
            <li key={entry.id}>
              <div className="reply-detail-heading">
                <strong>{entry.title}</strong>
                <span>{statusLabels[entry.status]}</span>
              </div>
              {entry.detail && <p>{entry.detail}</p>}
              {entry.reasoning && (
                <div className="reply-reasoning" aria-label="模型思考内容">
                  <ChatMarkdown content={entry.reasoning} />
                </div>
              )}
              {entry.tool_name && <code>{entry.tool_name}</code>}
              {entry.arguments !== undefined && (
                <div className="reply-tool-value">
                  <span>调用参数</span>
                  <pre tabIndex={0} aria-label="调用参数">
                    {JSON.stringify(entry.arguments, null, 2)}
                  </pre>
                </div>
              )}
              {entry.result !== undefined && (
                <div className="reply-tool-value">
                  <span>执行结果</span>
                  <pre tabIndex={0} aria-label="执行结果">
                    {JSON.stringify(entry.result, null, 2)}
                  </pre>
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </details>
  );
}

export const AgentReply = memo(function AgentReply({
  message,
  run,
}: {
  message: ChatMessage;
  run?: ChatRun;
}) {
  const entries = run?.workflow ?? message.workflow ?? [];
  const streaming = message.status === "streaming";
  const parts = replyParts(message.content, entries, streaming);
  // Runs created before workflow recording can still have business receipts.
  const receipts = run?.executions.filter(
    (receipt) =>
      !entries.some(
        (entry) =>
          entry.kind === "tool" &&
          entry.operation_id === receipt.operation_id &&
          entry.result !== undefined,
      ),
  );
  return (
    <div className="reply-flow chat-message-text">
      {parts.map((part, index) =>
        part.kind === "activity" ? (
          <ActivityDetails key={part.key} entries={part.entries} />
        ) : (
          <div key={part.key} className="reply-prose">
            <ChatMarkdown content={part.text} />
            {streaming && index === parts.length - 1 && (
              <span className="chat-stream-cursor" aria-hidden="true" />
            )}
          </div>
        ),
      )}
      {Boolean(receipts?.length) && (
        <details className="reply-activity">
          <summary>
            <Wrench size={14} aria-hidden="true" />
            工具执行结果
            <ChevronDown
              size={13}
              className="reply-chevron"
              aria-hidden="true"
            />
          </summary>
          <ul className="reply-activity-details">
            {receipts?.map((receipt) => (
              <li key={receipt.id}>
                {receipt.operation_id} ·{" "}
                {receipt.status === "succeeded"
                  ? "已完成"
                  : receipt.status === "failed"
                    ? "失败"
                    : "执行结果待核对"}
              </li>
            ))}
          </ul>
        </details>
      )}
      {streaming && !parts.length && <Spinner label="正在处理请求" />}
      {message.status === "cancelled" &&
        !entries.some((entry) => entry.status === "cancelled") && (
          <small role="status">本次任务已取消。</small>
        )}
      {message.status === "expired" && (
        <small role="status">本次待执行操作已过期。</small>
      )}
      {message.status === "complete" && !parts.length && !receipts?.length && (
        <small>本次没有收到回复内容</small>
      )}
    </div>
  );
});
