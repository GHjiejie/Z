import { useId, useState } from "react";
import { number } from "./api";
import { Button, Empty, ResourceState, useResource } from "./components";
import type { Dashboard, Run, User, Wallet } from "./types";
import { WorkspaceRunDetails, WorkspaceRunStatus, workspaceDate } from "./WorkspaceRuns";
import "./workspace-design.css";

// Wallet fields are conditional on billing.read in the existing API contract.
type Overview = Omit<Dashboard, keyof Wallet> & Partial<Wallet>;
type PrivateRuns = { items: Run[]; next_cursor: string | null };
const dollars = (value: string) => `$${Number(value).toFixed(4)}`;

export function WorkspaceOverview({ user, navigate }: { user: User; navigate: (path: string) => void }) {
  const overview = useResource<Overview>("/dashboard");
  const canReadRuns = user.capabilities?.includes("runs.read_own") ?? false;
  const canRun = user.capabilities?.includes("runs.execute") ?? false;
  const allUsage = user.capabilities?.includes("usage.read_all") ?? false;
  const runs = useResource<PrivateRuns>("/runs?limit=3", canReadRuns);
  const [runId, setRunId] = useState<string | null>(null);
  const value = overview.data;
  return <div className="workspace-page workspace-overview" data-testid="workspace-overview">
    <header className="workspace-title">
      <div><h1>概览</h1><p>{allUsage ? "当前组织调用概况与近期执行状态" : "我的调用概况与近期执行状态"}</p></div>
      {canRun && <Button onClick={() => navigate("playground")}><img src="/workspace-design/89845.svg" alt="" />开始对话</Button>}
    </header>
    <ResourceState loading={overview.loading} error={overview.error} retry={() => void overview.reload()}>
      {value && <>
        <div className="workspace-metrics" aria-label={allUsage ? "组织调用统计" : "我的调用统计"}>
          {[
            ["累计调用 (Requests)", number(value.requests)],
            ["累计 Tokens", number(value.tokens)],
            ["累计费用 (USD)", dollars(value.cost)],
            ["已确认调用占比", value.requests ? `${value.success_rate.toFixed(1)}%` : "—"],
            ["活跃运行 (Active)", number(value.active_runs)],
          ].map(([label, amount]) => <section className="workspace-metric" key={label}><span>{label}</span><strong>{amount}</strong></section>)}
        </div>
        <p className="workspace-total-note">全部已有记录；不是近30天总计</p>
        <div className="workspace-chart-grid">
          <section className="workspace-panel workspace-trend-panel">
            <header><h2>每日调用趋势</h2><span>最近最多30个有调用的日期 · Asia/Shanghai</span></header>
            {value.daily.length ? <DailyTrend daily={value.daily} /> : <Empty title="暂无调用记录" description="实际模型调用产生后，这里显示每日调用趋势。" />}
          </section>
          <section className="workspace-panel workspace-distribution">
            <header><h2>模型分布</h2><span>按实际调用统计</span></header>
            {value.models.length ? <table aria-label="模型分布">
              <colgroup><col /><col /><col /></colgroup>
              <thead><tr><th>模型</th><th>调用次数</th><th>占比</th></tr></thead>
              <tbody>{value.models.map(model => <tr key={model.model}><td><code title={model.model}>{model.model}</code></td><td>{number(model.requests)}</td><td>{value.requests ? `${(model.requests / value.requests * 100).toFixed(1)}%` : "—"}</td></tr>)}</tbody>
              <tfoot><tr><th>合计</th><td>{number(value.requests)}</td><td>{value.requests ? "100%" : "—"}</td></tr></tfoot>
            </table> : <Empty title="暂无模型用量" />}
          </section>
        </div>
        <section className="workspace-panel workspace-recent">
          <header><div><h2>我的近期运行</h2><span>仅显示你发起的最新运行</span></div>{canReadRuns && <Button variant="ghost" onClick={() => navigate("runs")}>查看全部 ›</Button>}</header>
          {canReadRuns ? <ResourceState loading={runs.loading} error={runs.error} retry={() => void runs.reload()}>
            {runs.data?.items.length ? <div className="workspace-recent-scroll"><table aria-label="我的近期运行">
              <colgroup><col /><col /><col /><col /></colgroup>
              <thead><tr><th>Agent</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead>
              <tbody>{runs.data.items.map(run => <tr key={run.id}><td>{run.agent_name || "Agent 运行"}</td><td><WorkspaceRunStatus compact status={run.status} /></td><td><time dateTime={run.created_at}>{workspaceDate(run.created_at)}</time></td><td><Button variant="ghost" onClick={() => setRunId(run.id)}>查看详情</Button></td></tr>)}</tbody>
            </table></div> : <Empty title="暂无运行记录" description="发送消息后，你的最新运行会显示在这里。" action={canRun ? <Button variant="secondary" onClick={() => navigate("playground")}>开始对话</Button> : undefined} />}
          </ResourceState> : <Empty title="无运行记录查看权限" />}
        </section>
        {value.blocked && <div className="workspace-operational-note" role="alert">钱包已暂停新的模型调用。{value.block_reason || "请联系管理员核对账务后处理。"}</div>}
        {!value.gateway_configured && !value.requests && <div className="workspace-operational-note"><span>模型网关尚未就绪，请联系平台运营人员完成配置。</span><Button variant="secondary" onClick={() => navigate("models")}>查看模型</Button></div>}
      </>}
    </ResourceState>
    {runId && <WorkspaceRunDetails runId={runId} close={() => setRunId(null)} navigate={navigate} canRun={canRun} updated={updated => runs.setData(previous => previous ? { ...previous, items: previous.items.map(run => run.id === updated.id ? { ...run, ...updated } : run) } : previous)} />}
  </div>;
}

function DailyTrend({ daily }: { daily: Dashboard["daily"] }) {
  const descriptionId = useId();
  const maximum = Math.max(1, ...daily.map(day => day.requests));
  const ceiling = Math.max(100, Math.ceil(maximum / 100) * 100);
  const baseline = 128.055;
  const graphTop = 12.315;
  const y = (amount: number) => baseline - amount / ceiling * (baseline - graphTop);
  const points = daily.map((day, index) => ({ ...day, x: daily.length === 1 ? 299 : 72.355 + index / (daily.length - 1) * 464.035, y: y(day.requests) }));
  const ticks = [ceiling * 5 / 6, ceiling / 2, ceiling / 6];
  // The design's example path is a data visual, never a static replacement for real calls.
  return <div className="workspace-trend-box">
    <div className="workspace-trend-canvas" role="img" aria-label="每日调用趋势，最近最多30个有调用的日期" aria-describedby={descriptionId}>
      {ticks.map(tick => <div key={tick}><img className="workspace-trend-gridline" src="/workspace-design/dbd63.svg" alt="" style={{ top: y(tick) - .482258 }} /><span className="workspace-trend-y" style={{ top: y(tick) - 6 }}>{number(Math.round(tick))}</span></div>)}
      <img className="workspace-trend-gridline" src="/workspace-design/ad706.svg" alt="" style={{ top: baseline - .482258 }} />
      <span className="workspace-trend-y" style={{ top: baseline - 6 }}>0</span>
      <svg className="workspace-trend-path" viewBox="0 0 598 150" preserveAspectRatio="none" aria-hidden="true"><polyline points={points.map(point => `${point.x},${point.y}`).join(" ")} fill="none" stroke="#6262e8" strokeWidth="1.92903" strokeLinecap="round" strokeLinejoin="round" /></svg>
      {points.map((point, index) => <div key={point.date}>
        <img className="workspace-trend-point" src="/workspace-design/8819c.svg" alt="" title={`${point.date}：${number(point.requests)} 次调用`} style={{ left: `${point.x / 598 * 100}%`, top: point.y }} />
        {(daily.length <= 10 || index === 0 || index === daily.length - 1 || index % Math.ceil(daily.length / 7) === 0) && <span className="workspace-trend-date" style={{ left: `${point.x / 598 * 100}%` }}>{point.date.slice(5).replace("-", "/")}</span>}
      </div>)}
    </div>
    <ul id={descriptionId} className="workspace-chart-readable-data">{daily.map(day => <li key={day.date}>{day.date}：{number(day.requests)} 次调用，{number(day.tokens)} Tokens，{dollars(day.cost)}</li>)}</ul>
  </div>;
}
