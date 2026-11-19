import { useEffect, useState, type ReactNode } from "react";
import { ArrowLeft, ChevronRight } from "lucide-react";
import { aborted, type ApiClient } from "./api";
import {
  auditLabel,
  auditOperationLabel,
  auditOutcomeLabels,
  auditStatus,
  auditTime,
} from "./auditLabels";
import type {
  AuditOperationDetail,
  AuditPermissionCheck,
} from "./auditRecords";
import { ErrorBanner, Modal, Spinner } from "./components";

const entryLabels: Readonly<Partial<Record<string, string>>> = {
  ag_ui: "控制台会话",
  chat_completions: "Chat Completions",
  chat_runs: "会话任务",
  direct_tool_api: "工具调用接口",
};
const dispatchLabels = {
  not_sent: "尚未派发",
  attempted: "已尝试派发",
  received: "业务接口已收到",
};
const stageLabels: Readonly<Partial<Record<string, string>>> = {
  runtime: "Agent 运行时",
  confirmation: "人工确认",
  tool_entry: "工具入口",
  business: "业务接口",
};
const checkLabels: Readonly<Partial<Record<string, string>>> = {
  authentication: "身份校验",
  user_grant: "工具授权",
  rbac: "RBAC 权限",
  grant_scope: "授权范围",
};
const decisionLabels = {
  allowed: "通过",
  denied: "拒绝",
  not_evaluated: "未校验",
  not_applicable: "不适用",
};
const eventLabels: Readonly<Partial<Record<string, string>>> = {
  requested: "申请调用工具",
  "attempt.started": "开始尝试",
  "permission.checked": "完成权限校验",
  waiting: "等待填写或确认",
  confirmed: "用户已确认",
  cancelled: "已取消",
  expired: "已过期",
  "attempt.rejected": "尝试被拒绝",
  "dispatch.attempted": "派发业务请求",
  "business.received": "业务接口收到请求",
  "read.succeeded": "业务读取成功",
  "write.committed": "业务变更已提交",
  "business.failed": "业务执行失败",
  "result.unknown": "结果待核对",
  "business.returned": "业务接口返回",
  "control.returned": "工具入口返回",
  "receipt.reconciled": "已核对执行回执",
};

type Fact = [label: string, value: ReactNode];

function Facts({ values }: { values: Fact[] }) {
  return (
    <dl className="audit-facts">
      {values.map(([label, value]) =>
        value == null ? null : (
          <div key={label}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ),
      )}
    </dl>
  );
}

function AuditTime({ stamp }: { stamp?: string }) {
  return stamp ? <time dateTime={stamp}>{auditTime(stamp)}</time> : null;
}

function PermissionCheck({
  evidence,
  stamp,
}: {
  evidence: AuditPermissionCheck;
  stamp?: string;
}) {
  const { stage, check, decision } = evidence;
  return (
    <li className="audit-permission">
      <div className="audit-permission-heading">
        <strong>
          {auditLabel(stageLabels, stage) ?? stage} ·{" "}
          {auditLabel(checkLabels, check) ?? check}
        </strong>
        <span className="audit-check-decision" data-decision={decision}>
          {decisionLabels[decision]}
        </span>
        <AuditTime stamp={stamp} />
      </div>
      <Facts
        values={[
          ["角色", evidence.roles?.join("、") || null],
          ["要求全部权限", evidence.required_all?.join("、") || null],
          ["要求任一权限", evidence.required_any?.join("、") || null],
          ["已满足", evidence.matched?.join("、") || null],
          ["缺少权限", evidence.missing?.join("、") || null],
          [
            "工具开关",
            evidence.grant_enabled === undefined
              ? null
              : evidence.grant_enabled
                ? "允许"
                : "未授权",
          ],
          ["授权版本", evidence.grant_version],
          ["授权修改人", evidence.grant_changed_by_user_id],
          [
            "授权修改时间",
            evidence.grant_changed_at && (
              <AuditTime stamp={evidence.grant_changed_at} />
            ),
          ],
        ]}
      />
    </li>
  );
}

function OperationDetails({
  detail,
  onChild,
}: {
  detail: AuditOperationDetail;
  onChild: (callId: string) => void;
}) {
  const eventTimes = new Map(
    detail.events.map((event) => [event.event_id, event.occurred_at]),
  );
  const references: Fact[] = [
    ["Agent ID", detail.agent_id],
    ["任务 ID", detail.run_id],
    ["执行 ID", detail.execution_id],
    ["工具入口请求 ID", detail.control_request_id],
    ["业务接口请求 ID", detail.business_request_id],
  ];
  return (
    <div className="audit-detail-content">
      <div className="audit-detail-summary">
        <div>
          <h3>{auditOperationLabel(detail.operation_id)}</h3>
          <code>
            {[detail.http_method, detail.route_template]
              .filter(Boolean)
              .join(" ") || detail.operation_id}
          </code>
        </div>
        <span
          className={`audit-status audit-status-${detail.business_outcome}`}
        >
          {auditStatus(detail)}
        </span>
      </div>
      <Facts
        values={[
          ["调用 ID", <code>{detail.call_id}</code>],
          ["发起时间", <AuditTime stamp={detail.started_at} />],
          ["操作者", detail.actor_user_id ?? "—"],
          ["权限所属用户", detail.subject_user_id ?? "—"],
          [
            "调用入口",
            detail.entrypoint &&
              (auditLabel(entryLabels, detail.entrypoint) ?? detail.entrypoint),
          ],
          ["实际目标", detail.target_id],
          [
            "请求目标",
            detail.requested_target_id !== detail.target_id
              ? detail.requested_target_id
              : null,
          ],
          ["确认人", detail.confirmed_by_user_id],
          [
            "确认时间",
            detail.confirmed_at && <AuditTime stamp={detail.confirmed_at} />,
          ],
          ["派发状态", dispatchLabels[detail.dispatch_state]],
          ["业务结果", auditOutcomeLabels[detail.business_outcome]],
          ["工具入口 HTTP", detail.control_http_status],
          ["业务接口 HTTP", detail.business_http_status],
          ["原因代码", detail.reason_code && <code>{detail.reason_code}</code>],
          [
            "结束时间",
            detail.finished_at && <AuditTime stamp={detail.finished_at} />,
          ],
          [
            "业务耗时",
            detail.duration_ms !== undefined
              ? `${detail.duration_ms} 毫秒`
              : null,
          ],
          ["返回条数", detail.result_summary?.returned_count],
          ["结果页码", detail.result_summary?.page],
          ["每页条数", detail.result_summary?.page_size],
          ["变更字段", detail.change_summary?.fields?.join("、") || null],
          [
            "变更摘要",
            detail.change_summary?.password_changed
              ? "密码已更新"
              : detail.change_summary?.no_change
                ? "没有字段变更"
                : null,
          ],
        ]}
      />

      <section className="audit-detail-section" aria-label="权限校验">
        <h3>权限校验</h3>
        {detail.permission_checks.length === 0 ? (
          <p className="field-hint">暂无权限校验记录</p>
        ) : (
          <ul className="audit-permissions">
            {detail.permission_checks.map((evidence, index) => (
              <PermissionCheck
                key={`${evidence.event_id ?? "unevaluated"}-${evidence.check}-${index}`}
                evidence={evidence}
                stamp={
                  evidence.event_id
                    ? eventTimes.get(evidence.event_id)
                    : undefined
                }
              />
            ))}
          </ul>
        )}
      </section>

      {detail.events.length > 0 && (
        <details className="audit-detail-section">
          <summary>
            执行事件 <small>{detail.events.length} 条</small>
          </summary>
          <ol className="audit-records">
            {detail.events.map((event) => (
              <li key={event.event_id}>
                <div>
                  <span>
                    {auditLabel(eventLabels, event.event_type) ??
                      event.event_type}
                  </span>
                  {event.reason_code && <code>{event.reason_code}</code>}
                  {event.event_type === "attempt.started" &&
                    event.attempt_id && <code>{event.attempt_id}</code>}
                </div>
                <AuditTime stamp={event.occurred_at} />
              </li>
            ))}
          </ol>
        </details>
      )}

      {detail.children.length > 0 && (
        <details className="audit-detail-section" open>
          <summary>
            关联调用 <small>{detail.children.length} 项</small>
          </summary>
          <ul className="audit-records">
            {detail.children.map((child) => (
              <li key={child.call_id}>
                <div>
                  <span>{auditOperationLabel(child.operation_id)}</span>
                  <code>{child.call_id}</code>
                </div>
                <button
                  type="button"
                  className="text-button audit-detail-link"
                  onClick={() => onChild(child.call_id)}
                >
                  查看详情 <ChevronRight size={14} aria-hidden="true" />
                </button>
              </li>
            ))}
          </ul>
        </details>
      )}

      {references.some(([, id]) => id != null) && (
        <details className="audit-detail-section audit-references">
          <summary>关联标识</summary>
          <Facts values={references} />
        </details>
      )}
    </div>
  );
}

type LoadResult =
  | { kind: "success"; detail: AuditOperationDetail }
  | { kind: "error"; error: unknown };

export function AuditOperationDialog({
  api,
  callId,
  onClose,
}: {
  api: ApiClient;
  callId: string;
  onClose: () => void;
}) {
  const [trail, setTrail] = useState([callId]);
  const [result, setResult] = useState<LoadResult | null>(null);
  const [revision, setRevision] = useState(0);
  const activeId = trail.at(-1) ?? callId;
  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void api
      .auditOperation(activeId, signal)
      .then((detail) => {
        if (!signal.aborted) setResult({ kind: "success", detail });
      })
      .catch((error) => {
        if (!signal.aborted && !aborted(error))
          setResult({ kind: "error", error });
      });
    return () => controller.abort();
  }, [activeId, api, revision]);

  return (
    <Modal
      title="操作详情"
      busy={false}
      onClose={onClose}
      className="audit-dialog"
      footer={
        <button type="button" className="button secondary" onClick={onClose}>
          关闭
        </button>
      }
    >
      {trail.length > 1 && (
        <button
          type="button"
          className="text-button audit-back"
          onClick={() => {
            setResult(null);
            setTrail((current) => current.slice(0, -1));
          }}
        >
          <ArrowLeft size={16} aria-hidden="true" /> 返回上级记录
        </button>
      )}
      {!result && (
        <div className="modal-loading">
          <Spinner label="正在加载操作详情" />
        </div>
      )}
      {result?.kind === "error" && (
        <ErrorBanner
          error={result.error}
          onRetry={() => {
            setResult(null);
            setRevision((current) => current + 1);
          }}
        />
      )}
      {result?.kind === "success" && (
        <OperationDetails
          key={activeId}
          detail={result.detail}
          onChild={(childId) => {
            setResult(null);
            setTrail((current) => [...current, childId]);
          }}
        />
      )}
    </Modal>
  );
}
