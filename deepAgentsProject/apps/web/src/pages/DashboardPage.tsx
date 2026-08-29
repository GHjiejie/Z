import {
  Activity,
  ArrowRight,
  Bot,
  CircleDollarSign,
  FileCheck2,
  Gauge,
  LibraryBig,
  MessageSquareText,
  PlayCircle,
  Plus,
  ServerCog,
  TriangleAlert,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { usePlatform } from '../context/PlatformContext'
import { ErrorBanner, LoadingBlock, MetricCard, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'

export function DashboardPage() {
  const { overview: data, context, loading, error } = usePlatform()
  if (loading) return <LoadingBlock />
  if (error && !data) return <ErrorBanner message={error} />
  if (!data) return <ErrorBanner message="Overview data is unavailable." />

  const activeRuns = (data.run_statuses.RUNNING ?? 0) + (data.run_statuses.PREPARING ?? 0) + (data.run_statuses.RESUMING ?? 0)
  const failedRuns = data.run_statuses.FAILED ?? 0
  const waitingForInput = data.run_statuses.WAITING_FOR_INPUT ?? 0
  const needsAttention = [
    data.pending_approvals ? { icon: FileCheck2, title: `${data.pending_approvals} approval${data.pending_approvals === 1 ? '' : 's'} waiting`, detail: 'Review policy-gated actions before they expire.', to: '/approvals', tone: 'amber' } : null,
    failedRuns ? { icon: TriangleAlert, title: `${failedRuns} failed run${failedRuns === 1 ? '' : 's'}`, detail: 'Open the trace to identify the last successful checkpoint.', to: '/runs?status=FAILED', tone: 'rose' } : null,
    waitingForInput ? { icon: MessageSquareText, title: `${waitingForInput} run${waitingForInput === 1 ? '' : 's'} waiting for input`, detail: 'Open Test & Run and provide the reviewer-requested changes.', to: '/runs?status=WAITING_FOR_INPUT', tone: 'blue' } : null,
    !data.deployments ? { icon: Bot, title: 'No active deployment', detail: 'Publish an agent revision and deploy it before starting a run.', to: '/agents', tone: 'violet' } : null,
  ].filter(Boolean) as Array<{ icon: typeof FileCheck2; title: string; detail: string; to: string; tone: string }>
  const showGettingStarted = data.agents === 0 || data.deployments === 0 || data.recent_runs.length === 0

  return (
    <div className="page-stack">
      <PageHeader eyebrow="PROJECT OVERVIEW" title={`Welcome to ${context?.project.name ?? 'your project'}`} description="Start with work that needs attention, then inspect live execution and recent activity." actions={<><Link className="button secondary" to="/agents"><Plus size={16} /> New agent</Link><Link className="button primary" to="/playground"><PlayCircle size={16} /> Start a run</Link></>} />

      {needsAttention.length > 0 && <section className="attention-section">
        <div className="section-heading"><div><h3>Needs attention</h3><p>Issues that can block publishing or execution.</p></div></div>
        <div className="attention-grid">{needsAttention.map(({ icon: Icon, title, detail, to, tone }) => <Link className="attention-card panel" to={to} key={title}><div className={`summary-icon tone-${tone}`}><Icon size={19} /></div><div><strong>{title}</strong><span>{detail}</span></div><ArrowRight size={17} /></Link>)}</div>
      </section>}

      <section className="metrics-grid">
        <MetricCard label="Active deployments" value={data.deployments} detail={`${data.agents} registered agents`} icon={Bot} tone="violet" />
        <MetricCard label="Active runs" value={activeRuns} detail={`${data.runtime.queue_depth} waiting in queue`} icon={Activity} tone="blue" />
        <MetricCard label="Success rate" value={data.success_rate === null ? '—' : `${data.success_rate}%`} detail={data.success_rate === null ? 'No completed runs yet' : 'Across completed runs'} icon={Gauge} tone="green" />
        <MetricCard label="Usage" value={data.usage.tokens.toLocaleString()} detail={`$${data.usage.cost.toFixed(4)} estimated`} icon={CircleDollarSign} tone="amber" />
      </section>

      {showGettingStarted && <section className="panel getting-started">
        <div className="panel-heading"><div><h3>Getting started</h3><p>Complete the core path once to verify this project is ready.</p></div></div>
        <div className="getting-started-list">
          <Link className={data.agents > 0 ? 'complete' : ''} to="/agents"><span>1</span><div><strong>Create an agent draft</strong><small>Define its purpose and model.</small></div><ArrowRight size={16} /></Link>
          <Link className={data.deployments > 0 ? 'complete' : ''} to="/agents"><span>2</span><div><strong>Validate, publish, and deploy</strong><small>Review the immutable revision before deployment.</small></div><ArrowRight size={16} /></Link>
          <Link className={data.recent_runs.length > 0 ? 'complete' : ''} to="/playground"><span>3</span><div><strong>Start a test run</strong><small>Observe events and inspect its output.</small></div><ArrowRight size={16} /></Link>
        </div>
      </section>}

      <section className="dashboard-grid">
        <div className="panel recent-runs-panel">
          <div className="panel-heading"><div><h3>Recent runs</h3><p>Latest execution activity across your agents</p></div><Link to="/runs" className="text-link">View all <ArrowRight size={14} /></Link></div>
          <div className="table-wrap"><table className="data-table"><thead><tr><th>Run</th><th>Agent</th><th>Status</th><th>Started</th><th>Plan</th></tr></thead><tbody>
            {data.recent_runs.length === 0 ? <tr><td colSpan={5} className="empty-cell">No runs yet. Start one in Test & Run.</td></tr> : data.recent_runs.map((run) => <tr key={run.id}>
              <td><Link className="run-cell" to={`/runs/${run.id}`}><div className="mini-run-icon"><PlayCircle size={15} /></div><div><strong>{shortId(run.id)}</strong><span>{run.input.slice(0, 46)}</span></div></Link></td>
              <td><strong className="table-strong">{run.agent_name}</strong><span className="table-sub">{run.environment}</span></td><td><StatusPill status={run.status} /></td><td><span className="table-time" title={new Date(run.created_at).toLocaleString()}>{formatRelative(run.created_at)}</span></td><td><code>{shortId(run.resolved_plan_id, 6)}</code></td>
            </tr>)}
          </tbody></table></div>
        </div>

        <div className="panel runtime-panel">
          <div className="panel-heading"><div><h3>Runtime health</h3><p>Execution plane status</p></div><StatusPill status={data.runtime.status} /></div>
          <div className="runtime-summary"><div className="runtime-summary-icon"><ServerCog size={25} /></div><strong>{data.runtime.workers}</strong><span>worker online</span><small>Updated {formatRelative(data.runtime.updated_at)}</small></div>
          <div className="runtime-stats"><div><span>Queue depth</span><strong>{data.runtime.queue_depth}</strong></div><div><span>Event lag</span><strong>{data.runtime.event_lag_ms === null ? 'Unavailable' : `${data.runtime.event_lag_ms}ms`}</strong></div><div><span>Pending HITL</span><strong>{data.pending_approvals}</strong></div></div>
        </div>
      </section>

      {data.agents > 0 && data.deployments > 0 && data.recent_runs.length > 0 && <section className="panel operational-hint"><LibraryBig size={20} /><div><strong>Improve answer quality with governed knowledge</strong><span>Bind an active knowledge revision to an agent, then verify citations in Test & Run.</span></div><Link className="button secondary" to="/knowledge">Open knowledge</Link></section>}
    </div>
  )
}
