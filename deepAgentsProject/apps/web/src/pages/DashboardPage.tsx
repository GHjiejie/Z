import {
  Activity,
  ArrowRight,
  Bot,
  Braces,
  CircleDollarSign,
  Clock3,
  FileCheck2,
  Gauge,
  Network,
  PlayCircle,
  Plus,
  ServerCog,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import type { Overview } from '../types'
import { ErrorBanner, LoadingBlock, MetricCard, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'

export function DashboardPage() {
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.overview().then(setData).catch((err) => setError(err.message))
  }, [])

  if (error) return <ErrorBanner message={error} />
  if (!data) return <LoadingBlock />

  const activeRuns = (data.run_statuses.RUNNING ?? 0) + (data.run_statuses.PREPARING ?? 0) + (data.run_statuses.RESUMING ?? 0)

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="CONTROL PLANE"
        title="Good morning, Zhengjie"
        description="Your agents are healthy. Here’s what’s happening across Project Atlas."
        actions={<><Link className="button secondary" to="/agents"><Plus size={16} /> New agent</Link><Link className="button primary" to="/playground"><PlayCircle size={16} /> Start a run</Link></>}
      />

      <section className="metrics-grid">
        <MetricCard label="Published agents" value={data.agents} detail={`${data.deployments} active deployments`} icon={Bot} tone="violet" delta="Ready" />
        <MetricCard label="Active runs" value={activeRuns} detail={`${data.runtime.queue_depth} waiting in queue`} icon={Activity} tone="blue" delta="Live" />
        <MetricCard label="Success rate" value={`${data.success_rate}%`} detail="Across completed runs" icon={Gauge} tone="green" delta="Healthy" />
        <MetricCard label="Usage this period" value={`${data.usage.tokens.toLocaleString()}`} detail={`$${data.usage.cost.toFixed(4)} estimated`} icon={CircleDollarSign} tone="amber" delta="On budget" />
      </section>

      <section className="dashboard-grid">
        <div className="panel recent-runs-panel">
          <div className="panel-heading">
            <div><h3>Recent runs</h3><p>Latest execution activity across your agents</p></div>
            <Link to="/runs" className="text-link">View all <ArrowRight size={14} /></Link>
          </div>
          <div className="table-wrap">
            <table className="data-table">
              <thead><tr><th>Run</th><th>Agent</th><th>Status</th><th>Started</th><th>Plan</th></tr></thead>
              <tbody>
                {data.recent_runs.length === 0 ? <tr><td colSpan={5} className="empty-cell">No runs yet. Start one in the Playground.</td></tr> : data.recent_runs.map((run) => (
                  <tr key={run.id}>
                    <td><div className="run-cell"><div className="mini-run-icon"><PlayCircle size={15} /></div><div><strong>{shortId(run.id)}</strong><span>{run.input.slice(0, 46)}</span></div></div></td>
                    <td><strong className="table-strong">{run.agent_name}</strong><span className="table-sub">{run.environment}</span></td>
                    <td><StatusPill status={run.status} /></td>
                    <td><span className="table-time">{formatRelative(run.created_at)}</span></td>
                    <td><code>{shortId(run.resolved_plan_id, 6)}</code></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel runtime-panel">
          <div className="panel-heading"><div><h3>Runtime health</h3><p>Execution plane status</p></div><StatusPill status={data.runtime.status} /></div>
          <div className="runtime-orbit">
            <div className="orbit-ring ring-one" />
            <div className="orbit-ring ring-two" />
            <div className="orbit-core"><ServerCog size={26} /><strong>{data.runtime.workers}</strong><span>worker online</span></div>
            <span className="orbit-node node-a"><Braces size={14} /></span>
            <span className="orbit-node node-b"><ShieldCheck size={14} /></span>
            <span className="orbit-node node-c"><Network size={14} /></span>
          </div>
          <div className="runtime-stats">
            <div><span>Queue depth</span><strong>{data.runtime.queue_depth}</strong></div>
            <div><span>Event lag</span><strong>{data.runtime.event_lag_ms}ms</strong></div>
            <div><span>Pending HITL</span><strong>{data.pending_approvals}</strong></div>
          </div>
        </div>
      </section>

      <section className="architecture-strip">
        <div className="architecture-copy"><span className="section-kicker"><Sparkles size={13} /> Execution path</span><h3>One immutable chain, complete traceability</h3><p>Every run is pinned to its revision, plan hash, runtime image, and execution attempt.</p></div>
        <div className="architecture-flow">
          {[
            ['Draft', 'Editable config'],
            ['Revision', 'Immutable snapshot'],
            ['Plan', 'Dependencies locked'],
            ['Worker', 'Runtime bound'],
            ['Events', 'Auditable output'],
          ].map(([title, subtitle], index) => <div className="flow-item" key={title}><span>{index + 1}</span><div><strong>{title}</strong><small>{subtitle}</small></div>{index < 4 && <ArrowRight size={15} />}</div>)}
        </div>
      </section>
    </div>
  )
}

