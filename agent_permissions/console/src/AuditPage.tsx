import { useEffect, useState, type FormEvent } from "react";
import { ChevronLeft, ChevronRight, RefreshCw, Search } from "lucide-react";
import { aborted, type ApiClient } from "./api";
import {
  auditOperationLabel,
  auditOutcomeLabels,
  auditStatus,
  auditTime,
} from "./auditLabels";
import { AuditOperationDialog } from "./AuditOperationDialog";
import {
  auditOutcome,
  type AuditGrantChange,
  type AuditOperation,
  type AuditOutcome,
  type AuditQuery,
} from "./auditRecords";
import { EmptyState, ErrorBanner, Field, Spinner } from "./components";

type AuditView = "operations" | "grants";
type Filters = Omit<AuditQuery, "cursor" | "limit"> & {
  business_outcome?: AuditOutcome;
};
interface ListRequest {
  view: AuditView;
  filters: Filters;
  cursors: string[];
}
type AuditList =
  | { view: "operations"; items: AuditOperation[]; next_cursor: string | null }
  | { view: "grants"; items: AuditGrantChange[]; next_cursor: string | null };
type ListResponse = { request: ListRequest } & (
  { kind: "success"; list: AuditList } | { kind: "error"; error: unknown }
);

export function AuditPage({ api }: { api: ApiClient }) {
  const [request, setRequest] = useState<ListRequest>({
    view: "operations",
    filters: {},
    cursors: [],
  });
  const [response, setResponse] = useState<ListResponse | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const resolved = response?.request === request ? response : null;
  const loading = resolved === null;
  const list = resolved?.kind === "success" ? resolved.list : null;
  const next = list?.next_cursor;
  const refresh = () => setRequest((current) => ({ ...current }));

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    const query = {
      ...request.filters,
      limit: 20,
      cursor: request.cursors.at(-1),
    };
    async function load() {
      try {
        const list: AuditList =
          request.view === "operations"
            ? {
                view: "operations",
                ...(await api.auditOperations(query, signal)),
              }
            : {
                view: "grants",
                ...(await api.auditGrantChanges(query, signal)),
              };
        if (!signal.aborted) setResponse({ request, kind: "success", list });
      } catch (error) {
        if (!signal.aborted && !aborted(error)) {
          setResponse({ request, kind: "error", error });
        }
      }
    }
    void load();
    return () => controller.abort();
  }, [api, request]);

  function filter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const outcome = auditOutcome.safeParse(fields.get("business_outcome"));
    setRequest({
      view: request.view,
      filters: {
        operation_id: String(fields.get("operation_id") ?? "").trim(),
        actor_user_id: String(fields.get("actor_user_id") ?? "").trim(),
        subject_user_id: String(fields.get("subject_user_id") ?? "").trim(),
        business_outcome: outcome.data,
      },
      cursors: [],
    });
  }

  const showingOperations = request.view === "operations";
  const viewLabel = showingOperations ? "操作记录" : "授权变更";
  return (
    <>
      <header className="page-heading audit-heading">
        <div>
          <h1>Agent 操作审计</h1>
          <p>查询业务调用、执行时的权限校验和工具授权变更</p>
        </div>
        <button
          type="button"
          className="button secondary"
          disabled={loading}
          onClick={refresh}
        >
          <RefreshCw size={16} aria-hidden="true" /> 刷新记录
        </button>
      </header>

      <section className="panel audit-panel" aria-label={viewLabel}>
        <div className="audit-views" role="group" aria-label="审计记录类型">
          {(["operations", "grants"] as const).map((view) => (
            <button
              key={view}
              type="button"
              className="audit-view"
              aria-pressed={request.view === view}
              onClick={() => {
                if (request.view !== view)
                  setRequest({ view, filters: {}, cursors: [] });
              }}
            >
              {view === "operations" ? "操作记录" : "授权变更"}
            </button>
          ))}
        </div>

        <form key={request.view} className="audit-filters" onSubmit={filter}>
          <AuditIdField
            name="operation_id"
            label="操作 ID"
            placeholder="如 user.list"
          />
          <AuditIdField
            name="actor_user_id"
            label="操作者 ID"
            placeholder="如 usr_…"
          />
          {showingOperations ? (
            <Field label="业务结果">
              <select
                className="select-control"
                name="business_outcome"
                defaultValue=""
              >
                <option value="">全部结果</option>
                {auditOutcome.options.map((value) => (
                  <option key={value} value={value}>
                    {auditOutcomeLabels[value]}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <AuditIdField
              name="subject_user_id"
              label="被授权用户 ID"
              placeholder="如 usr_…"
            />
          )}
          <button type="submit" className="button primary">
            <Search size={16} aria-hidden="true" /> 查询
          </button>
        </form>

        <div aria-busy={loading}>
          {loading && (
            <div className="table-loading">
              <Spinner label={`正在加载${viewLabel}`} />
            </div>
          )}
          {resolved?.kind === "error" && (
            <div className="list-error">
              <ErrorBanner error={resolved.error} onRetry={refresh} />
            </div>
          )}
          {list && (
            <>
              {list.items.length === 0 ? (
                <EmptyState
                  title={`暂无${viewLabel}`}
                  description="可调整筛选条件后重新查询"
                />
              ) : (
                <div
                  className="table-scroll"
                  role="region"
                  tabIndex={0}
                  aria-label={`${viewLabel}列表`}
                >
                  {list.view === "operations" ? (
                    <OperationTable items={list.items} onSelect={setSelected} />
                  ) : (
                    <GrantTable items={list.items} />
                  )}
                </div>
              )}
              <footer className="pagination">
                <span>第 {request.cursors.length + 1} 页 · 时间为北京时间</span>
                <div>
                  <button
                    type="button"
                    className="button secondary compact"
                    disabled={!request.cursors.length}
                    onClick={() =>
                      setRequest({
                        ...request,
                        cursors: request.cursors.slice(0, -1),
                      })
                    }
                  >
                    <ChevronLeft size={16} aria-hidden="true" /> 上一页
                  </button>
                  <button
                    type="button"
                    className="button secondary compact"
                    disabled={!next}
                    onClick={() => {
                      if (next)
                        setRequest({
                          ...request,
                          cursors: [...request.cursors, next],
                        });
                    }}
                  >
                    下一页 <ChevronRight size={16} aria-hidden="true" />
                  </button>
                </div>
              </footer>
            </>
          )}
        </div>
      </section>
      {selected && (
        <AuditOperationDialog
          api={api}
          callId={selected}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}

function AuditIdField({
  name,
  label,
  placeholder,
}: {
  name: string;
  label: string;
  placeholder: string;
}) {
  return (
    <Field label={label}>
      <input
        name={name}
        placeholder={placeholder}
        maxLength={128}
        pattern={"[A-Za-z0-9_.\\-]+"}
        title="可以使用字母、数字、点、下划线和连字符"
      />
    </Field>
  );
}

function OperationTable({
  items,
  onSelect,
}: {
  items: AuditOperation[];
  onSelect: (id: string) => void;
}) {
  return (
    <table className="audit-table">
      <caption className="sr-only">Agent 业务操作记录</caption>
      <thead>
        <tr>
          <th scope="col">发起时间</th>
          <th scope="col">操作</th>
          <th scope="col">操作者</th>
          <th scope="col">状态</th>
          <th scope="col">
            <span className="sr-only">详情</span>
          </th>
        </tr>
      </thead>
      <tbody>
        {items.map((operation) => (
          <tr key={operation.call_id}>
            <td>
              <time dateTime={operation.started_at}>
                {auditTime(operation.started_at)}
              </time>
            </td>
            <OperationName operationId={operation.operation_id} />
            <td className="audit-identity">
              <code>{operation.actor_user_id ?? "—"}</code>
            </td>
            <td>
              <span
                className={`audit-status audit-status-${operation.business_outcome}`}
              >
                {auditStatus(operation)}
              </span>
            </td>
            <td>
              <button
                type="button"
                className="text-button audit-detail-link"
                aria-label={`查看详情 ${operation.call_id}`}
                onClick={() => onSelect(operation.call_id)}
              >
                查看详情 <ChevronRight size={14} aria-hidden="true" />
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function GrantTable({ items }: { items: AuditGrantChange[] }) {
  return (
    <table className="audit-table">
      <caption className="sr-only">Agent 工具授权变更</caption>
      <thead>
        <tr>
          <th scope="col">变更时间</th>
          <th scope="col">工具</th>
          <th scope="col">修改人</th>
          <th scope="col">被授权用户</th>
          <th scope="col">变更结果</th>
        </tr>
      </thead>
      <tbody>
        {items.map((grant) => (
          <tr key={grant.event_id}>
            <td>
              <time dateTime={grant.occurred_at}>
                {auditTime(grant.occurred_at)}
              </time>
            </td>
            <OperationName operationId={grant.operation_id} />
            <td className="audit-identity">
              <code>{grant.actor_user_id}</code>
            </td>
            <td className="audit-identity">
              <code>{grant.subject_user_id}</code>
            </td>
            <td className="audit-grant-change">
              <span>
                {grant.change_summary.before ? "允许" : "未授权"} →{" "}
                {grant.change_summary.after ? "允许" : "未授权"}
              </span>
              <small>授权版本 {grant.grant_version}</small>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function OperationName({ operationId }: { operationId?: string }) {
  const label = auditOperationLabel(operationId);
  return (
    <td className="audit-operation-name">
      <strong>{label}</strong>
      {operationId && operationId !== label && <code>{operationId}</code>}
    </td>
  );
}
