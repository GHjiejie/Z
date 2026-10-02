import { useEffect, useRef, useState } from "react";
import { api, getScopeRevision, getTenantId, money, scopeIsCurrent } from "./api";
import { Button, Empty, Modal, ResourceState, useResource, useToast } from "./components";
import type { Run } from "./types";
import "./workspace-runs.css";

type WorkspaceRun = Run & { finished_at?: string | null; message?: string };
type RunPage = { items: WorkspaceRun[]; next_cursor: string | null };
type Navigate = (path: string) => void;
const activeStatuses = new Set(["queued", "running", "cancelling"]);
const runLabels: Record<string, string> = {
  queued: "排队中", running: "运行中", cancelling: "取消中", succeeded: "成功",
  failed: "失败", cancelled: "已取消", expired: "已超时",
  completed: "成功", canceled: "已取消", interrupted: "已中断",
};
const errorMessage = (error: unknown) => error instanceof Error ? error.message : "请求失败，请重试。";
const scopeError = (error: unknown) => (error as { code?: string })?.code === "scope_changed";
const runMoney = (cost?: string) => money(cost, 4).replace("US$", "$");

/** Format the workspace's dates in its reporting timezone, Asia/Shanghai. */
export function workspaceDate(value: string) {
  const timestamp = new Date(/(?:Z|[+-]\d{2}:\d{2})$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(timestamp.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(timestamp).replace("/", "-");
}

/** The Figma run status badge, with a readable fallback for new service statuses. */
export function WorkspaceRunStatus({ status, compact = false }: { status: string; compact?: boolean }) {
  const tone = status === "running" ? "running"
    : ["queued", "cancelling"].includes(status) ? "pending"
      : ["failed", "expired", "interrupted"].includes(status) ? "failed"
        : ["succeeded", "completed"].includes(status) ? "succeeded" : "neutral";
  const label = runLabels[status] ?? status;
  return <span className={`workspace-run-status workspace-run-status-${tone}${compact ? " workspace-run-status-compact" : ""}`} title={status}>
    <i aria-hidden="true" /><span>{label}{label !== status && (compact ? <> ({status})</> : <> {status}</>)}</span>
  </span>;
}

/** Private run history backed by the real cursor endpoint; search covers loaded rows. */
export function WorkspaceRuns({ navigate, canRun }: { navigate: Navigate; canRun: boolean }) {
  const resource = useResource<RunPage>("/runs?limit=50");
  const [search, setSearch] = useState("");
  const [loadingMore, setLoadingMore] = useState(false);
  const [appendError, setAppendError] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const scope = useRef(getTenantId()).current;
  const scopeRevision = useRef(getScopeRevision()).current;
  const alive = useRef(true);
  const requestRevision = useRef(0);
  const appendRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    alive.current = true;
    const stop = () => {
      alive.current = false;
      requestRevision.current++;
      appendRequest.current?.abort();
    };
    window.addEventListener("tenant-scope-changed", stop);
    return () => { stop(); window.removeEventListener("tenant-scope-changed", stop); };
  }, []);

  function refresh() {
    requestRevision.current++;
    appendRequest.current?.abort();
    appendRequest.current = null;
    setLoadingMore(false);
    setAppendError("");
    resource.setData(null);
    void resource.reload();
  }

  async function loadMore() {
    const cursor = resource.data?.next_cursor;
    if (!cursor || appendRequest.current || resource.loading || !alive.current) return;
    const controller = new AbortController();
    appendRequest.current = controller;
    const revision = requestRevision.current;
    setLoadingMore(true);
    setAppendError("");
    try {
      const next = await api<RunPage>(`/runs?limit=50&cursor=${encodeURIComponent(cursor)}`, { signal: controller.signal }, scope);
      if (!alive.current || controller.signal.aborted || revision !== requestRevision.current || !scopeIsCurrent(scopeRevision)) return;
      resource.setData(previous => {
        if (!previous || previous.next_cursor !== cursor) return previous;
        const seen = new Set(previous.items.map(run => run.id));
        const unique = next.items.filter(run => {
          if (seen.has(run.id)) return false;
          seen.add(run.id);
          return true;
        });
        return { items: [...previous.items, ...unique], next_cursor: next.next_cursor };
      });
    } catch (error) {
      if (alive.current && !controller.signal.aborted && revision === requestRevision.current && !scopeError(error)) setAppendError(errorMessage(error));
    } finally {
      if (appendRequest.current === controller) {
        appendRequest.current = null;
        if (alive.current && revision === requestRevision.current) setLoadingMore(false);
      }
    }
  }

  const items = resource.data?.items ?? [];
  const term = search.trim().toLocaleLowerCase();
  const visible = term ? items.filter(run => [run.agent_name, run.id, run.model_alias, run.model_id, run.status, runLabels[run.status]]
    .some(value => value?.toLocaleLowerCase().includes(term))) : items;
  const updateRun = (updated: Run) => resource.setData(previous => previous ? {
    ...previous, items: previous.items.map(run => run.id === updated.id ? { ...run, ...updated } : run),
  } : previous);

  return <div className="workspace-page workspace-runs-page">
    <header className="workspace-title">
      <div><h1>我的运行记录</h1><p>仅显示你发起的运行</p></div>
      <Button variant="secondary" className="workspace-run-refresh" onClick={refresh} disabled={resource.loading}>
        <img src="/workspace-design/42eff.svg" alt="" />刷新
      </Button>
    </header>
    <div className="workspace-run-toolbar">
      <label className="workspace-run-search">
        <img src="/workspace-design/7f43b.svg" alt="" />
        <input aria-label="筛选已加载的运行记录" placeholder="筛选已加载记录..." value={search} onChange={event => setSearch(event.target.value)} />
      </label>
    </div>
    <section className="workspace-panel workspace-run-card" aria-label="我的运行记录">
      <ResourceState loading={resource.loading} error={resource.error} retry={refresh}>
        {visible.length ? <div className="workspace-run-table-scroll">
          <table className="workspace-run-table">
            <colgroup><col /><col /><col /><col /><col /><col /></colgroup>
            <thead><tr><th>Agent / Run ID</th><th>状态</th><th>模型</th><th>客户费用</th><th>创建时间</th><th>操作</th></tr></thead>
            <tbody>{visible.map(run => <tr key={run.id}>
              <td><div className="workspace-run-identity"><strong>{run.agent_name || "Agent 运行"}</strong><span title={run.id}>{run.id}</span></div></td>
              <td><WorkspaceRunStatus status={run.status} /></td>
              <td>{run.model_alias || run.model_id ? <span className="workspace-run-model" title={run.model_alias || run.model_id}>{run.model_alias || run.model_id}</span> : <span className="workspace-run-unavailable">—</span>}</td>
              <td><div className="workspace-run-cost" title={run.cost == null ? undefined : `${run.cost} USD`}><span>{runMoney(run.cost)}</span>{activeStatuses.has(run.status) && <small>当前计量</small>}</div></td>
              <td className="workspace-run-date"><time dateTime={run.created_at}>{workspaceDate(run.created_at)}</time></td>
              <td><Button variant="ghost" className="workspace-run-detail-link" onClick={() => setRunId(run.id)}>查看详情</Button></td>
            </tr>)}</tbody>
          </table>
        </div> : <Empty title={term ? "当前已加载记录无匹配" : "暂无运行记录"}
          description={term ? "试试其他关键词，或加载更多记录后继续筛选。" : "发送消息后，你的 Agent 运行会显示在这里。"}
          action={!term && canRun ? <Button variant="secondary" onClick={() => navigate("playground")}>打开对话实验室</Button> : undefined} />}
        {appendError && <div className="workspace-run-append-error" role="alert"><span>{appendError}</span><Button variant="secondary" onClick={() => void loadMore()} busy={loadingMore}>重试</Button></div>}
        <footer className="workspace-run-footer">
          <span>已加载 {items.length} 条{term ? ` · 匹配 ${visible.length} 条` : ""}</span>
          <Button variant="secondary" className="workspace-run-load-more" busy={loadingMore} disabled={!resource.data?.next_cursor} title={resource.data?.next_cursor ? undefined : "当前已加载全部记录"} onClick={() => void loadMore()}>加载更多</Button>
        </footer>
      </ResourceState>
    </section>
    {runId && <WorkspaceRunDetails runId={runId} close={() => setRunId(null)} navigate={navigate} canRun={canRun} updated={updateRun} />}
  </div>;
}

type RunDetailsProps = { runId: string; close: () => void; navigate: Navigate; canRun: boolean; updated?: (run: Run) => void };

/** Detail layout is inferred: Figma supplies the detail entry, but no detail artboard. */
export function WorkspaceRunDetails(props: RunDetailsProps) {
  return <WorkspaceRunDetailsContent key={props.runId} {...props} />;
}

function WorkspaceRunDetailsContent({ runId, close, navigate, canRun, updated }: RunDetailsProps) {
  const resource = useResource<WorkspaceRun>(`/runs/${encodeURIComponent(runId)}`);
  const [canceling, setCanceling] = useState(false);
  const [operationError, setOperationError] = useState("");
  const [pollError, setPollError] = useState("");
  const scope = useRef(getTenantId()).current;
  const scopeRevision = useRef(getScopeRevision()).current;
  const alive = useRef(true);
  const cancelBusy = useRef(false);
  const requestRevision = useRef(0);
  const cancelRequest = useRef<AbortController | null>(null);
  const pollRequest = useRef<AbortController | null>(null);
  const updatedRef = useRef(updated);
  updatedRef.current = updated;
  const toast = useToast();

  function stopRequests() {
    alive.current = false;
    requestRevision.current++;
    cancelRequest.current?.abort();
    pollRequest.current?.abort();
  }

  useEffect(() => {
    alive.current = true;
    window.addEventListener("tenant-scope-changed", stopRequests);
    return () => { stopRequests(); window.removeEventListener("tenant-scope-changed", stopRequests); };
  }, []);

  useEffect(() => {
    if (resource.data && alive.current && scopeIsCurrent(scopeRevision)) updatedRef.current?.(resource.data);
  }, [resource.data, scopeRevision]);

  useEffect(() => {
    if (!resource.data || !activeStatuses.has(resource.data.status) || resource.loading) return;
    const timer = window.setInterval(() => {
      if (!alive.current || cancelBusy.current || pollRequest.current || !scopeIsCurrent(scopeRevision)) return;
      const controller = new AbortController();
      pollRequest.current = controller;
      const revision = requestRevision.current;
      void api<WorkspaceRun>(`/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }, scope)
        .then(run => {
          if (alive.current && !controller.signal.aborted && revision === requestRevision.current && scopeIsCurrent(scopeRevision)) {
            resource.setData(run);
            setPollError("");
          }
        }).catch(error => {
          if (alive.current && !controller.signal.aborted && revision === requestRevision.current && !scopeError(error)) setPollError("状态更新暂时失败，请手动刷新运行详情。");
        }).finally(() => { if (pollRequest.current === controller) pollRequest.current = null; });
    }, 5000);
    return () => { window.clearInterval(timer); pollRequest.current?.abort(); pollRequest.current = null; };
  }, [runId, resource.data?.status, resource.loading, resource.setData, scope, scopeRevision]);

  function dismiss() { stopRequests(); close(); }

  function refresh() {
    requestRevision.current++;
    pollRequest.current?.abort();
    pollRequest.current = null;
    setPollError("");
    setOperationError("");
    void resource.reload();
  }

  async function cancel() {
    const run = resource.data;
    if (!run || !canRun || !["queued", "running"].includes(run.status) || cancelBusy.current || !alive.current) return;
    cancelBusy.current = true;
    requestRevision.current++;
    pollRequest.current?.abort();
    pollRequest.current = null;
    const revision = requestRevision.current;
    const controller = new AbortController();
    cancelRequest.current = controller;
    setCanceling(true);
    setOperationError("");
    try {
      const result = await api<WorkspaceRun>(`/runs/${encodeURIComponent(run.id)}/cancel`, { method: "POST", signal: controller.signal }, scope);
      if (!alive.current || controller.signal.aborted || revision !== requestRevision.current || !scopeIsCurrent(scopeRevision)) return;
      resource.setData(previous => previous ? { ...previous, ...result } : result);
      toast(result.status === "cancelled" ? "运行已取消" : result.status === "cancelling" ? "已请求取消，等待运行停止" : "运行状态已更新");
    } catch (error) {
      if (alive.current && !controller.signal.aborted && revision === requestRevision.current && !scopeError(error)) setOperationError(errorMessage(error));
    } finally {
      if (cancelRequest.current === controller) {
        cancelRequest.current = null;
        cancelBusy.current = false;
        if (alive.current && revision === requestRevision.current) setCanceling(false);
      }
    }
  }

  const run = resource.data;
  return <div className="workspace-run-modal">
    <Modal title="运行详情" description="查看本次运行的状态、输入与客户费用。" close={dismiss}>
      <ResourceState loading={resource.loading} error={resource.error} retry={refresh}>
        {run && <>
          <div className="workspace-run-detail-body">
            <div className="workspace-run-detail-summary"><h3>{run.agent_name || "Agent 运行"}</h3><WorkspaceRunStatus status={run.status} /></div>
            <dl className="workspace-run-detail-fields">
              <div><dt>运行 ID</dt><dd><code>{run.id}</code><Button variant="ghost" className="workspace-run-copy" onClick={async () => {
                try { await navigator.clipboard.writeText(run.id); if (alive.current) toast("运行 ID 已复制"); }
                catch { if (alive.current) toast("浏览器暂不支持复制", "error"); }
              }}>复制</Button></dd></div>
              <div><dt>模型</dt><dd>{run.model_alias || run.model_id || "—"}</dd></div>
              <div><dt>客户费用</dt><dd title={run.cost == null ? undefined : `${run.cost} USD`}>{runMoney(run.cost)}{activeStatuses.has(run.status) && <small>当前计量</small>}</dd></div>
              <div><dt>创建时间</dt><dd>{workspaceDate(run.created_at)}</dd></div>
              <div><dt>结束时间</dt><dd>{run.finished_at ? workspaceDate(run.finished_at) : "—"}</dd></div>
            </dl>
            {run.message && <section className="workspace-run-input"><h4>输入消息</h4><p>{run.message}</p></section>}
            {run.error && <div className="workspace-run-detail-error" role="alert"><strong>运行提示</strong><p>{run.error}</p></div>}
            {run.status === "cancelling" && <p className="workspace-run-cancelling" role="status">已请求取消，正在等待运行停止。</p>}
            {(operationError || pollError) && <p className="workspace-run-detail-error" role="alert">{operationError || pollError}</p>}
          </div>
          <footer className="workspace-run-detail-footer">
            <Button variant="secondary" onClick={refresh} disabled={canceling || resource.loading}>刷新状态</Button>
            {canRun && activeStatuses.has(run.status) && <Button variant="danger" busy={canceling} disabled={run.status === "cancelling"} onClick={() => void cancel()}>{run.status === "cancelling" ? "取消中" : "取消运行"}</Button>}
            {canRun && <Button onClick={() => { dismiss(); navigate(`playground?session=${encodeURIComponent(run.session_id)}`); }}>查看会话</Button>}
          </footer>
        </>}
      </ResourceState>
    </Modal>
  </div>;
}
