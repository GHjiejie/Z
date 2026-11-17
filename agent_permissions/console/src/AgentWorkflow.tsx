import { useState } from "react";
import {
  Brain,
  Check,
  ChevronDown,
  Circle,
  CircleAlert,
  ListChecks,
  LoaderCircle,
  Pause,
  Wrench,
  X,
} from "lucide-react";
import type { WorkflowEntry } from "./workflow";
import { ChatMarkdown } from "./ChatMarkdown";

const statusLabels: Record<WorkflowEntry["status"], string> = {
  pending: "待执行",
  running: "进行中",
  waiting: "等待你处理",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  interrupted: "已中断",
};

function StatusIcon({ entry }: { entry: WorkflowEntry }) {
  if (entry.status === "running")
    return <LoaderCircle size={14} className="spin" />;
  if (entry.status === "succeeded") return <Check size={14} />;
  if (entry.status === "failed") return <CircleAlert size={14} />;
  if (entry.status === "cancelled") return <X size={14} />;
  if (entry.status === "waiting" || entry.status === "interrupted")
    return <Pause size={14} />;
  return <Circle size={12} />;
}

export function AgentWorkflow({ entries }: { entries: WorkflowEntry[] }) {
  const [open, setOpen] = useState(true);
  if (!entries.length) return null;
  const current =
    entries.find((entry) => entry.status === "running") ??
    entries.find((entry) => entry.status === "waiting") ??
    entries.at(-1);
  return (
    <details
      className="agent-workflow"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>
        <ListChecks size={16} aria-hidden="true" />
        <span>执行过程</span>
        <span className="workflow-current" role="status">
          {current?.title} · {current ? statusLabels[current.status] : ""}
        </span>
        <ChevronDown size={16} aria-hidden="true" />
      </summary>
      <ol className="workflow-entries" aria-label="工作流步骤">
        {entries.map((entry) => (
          <li
            className={`workflow-entry is-${entry.status}`}
            aria-current={
              entry.status === "running" || entry.status === "waiting"
                ? "step"
                : undefined
            }
            key={entry.id}
          >
            <span className="workflow-status-icon" aria-hidden="true">
              <StatusIcon entry={entry} />
            </span>
            <div className="workflow-entry-content">
              <div className="workflow-entry-heading">
                {entry.kind === "model" ? (
                  <Brain size={14} aria-hidden="true" />
                ) : entry.kind === "tool" ? (
                  <Wrench size={14} aria-hidden="true" />
                ) : null}
                <strong>{entry.title}</strong>
                <span>{statusLabels[entry.status]}</span>
              </div>
              <p className="workflow-entry-detail">{entry.detail}</p>
              {entry.tool_name && (
                <code className="workflow-tool-name">{entry.tool_name}</code>
              )}
              {entry.kind === "model" &&
                (entry.reasoning ? (
                  <div className="workflow-reasoning" aria-label="模型思考内容">
                    <span>思考摘要</span>
                    <ChatMarkdown content={entry.reasoning} />
                  </div>
                ) : entry.status === "succeeded" ? (
                  <p className="workflow-reasoning-empty">
                    模型未返回可展示的思考摘要
                  </p>
                ) : null)}
              {(entry.arguments !== undefined ||
                entry.result !== undefined) && (
                <div className="workflow-tool-data">
                  {entry.arguments !== undefined && (
                    <details>
                      <summary>调用参数</summary>
                      <pre>{JSON.stringify(entry.arguments, null, 2)}</pre>
                    </details>
                  )}
                  {entry.result !== undefined && (
                    <details>
                      <summary>执行结果</summary>
                      <pre>{JSON.stringify(entry.result, null, 2)}</pre>
                    </details>
                  )}
                </div>
              )}
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}
