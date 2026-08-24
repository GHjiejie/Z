import { Activity, CalendarClock, CircleDollarSign, Clock3, Filter, GitBranch, PlayCircle, RefreshCw, Search, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { EventTimeline } from '../components/EventTimeline'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'
import { api } from '../lib/api'
import type { Run, RuntimeEvent } from '../types'

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Run | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)

  const load = () => api.runs().then(({ items }) => { setRuns(items); setLoaded(true) }).catch((err) => { setError(err.message); setLoaded(true) })
  useEffect(() => { void load() }, [])

  const inspect = async (run: Run) => {
    setSelected(await api.run(run.id))
    setEvents((await api.runEvents(run.id)).items)
  }

  const visible = runs.filter((run) => `${run.id} ${run.agent_name} ${run.input} ${run.status}`.toLowerCase().includes(search.toLowerCase()))
  if (!loaded) return <LoadingBlock />

  return <div className="page-stack">
    <PageHeader eyebrow="OBSERVABILITY" title="Every run, attempt, and event" description="Inspect execution state without conflating checkpoints, scheduling status, and audit events." actions={<button className="button secondary" onClick={load}><RefreshCw size={15} /> Refresh</button>} />
    {error && <ErrorBanner message={error} />}
    <div className="panel runs-panel">
      <div className="table-toolbar"><div className="table-search"><Search size={16} /><input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search runs, agents, status…" /></div><button className="button ghost"><Filter size={15} /> Filters</button><span>{visible.length} runs</span></div>
      <div className="table-wrap"><table className="data-table runs-table"><thead><tr><th>Run & request</th><th>Agent</th><th>Status</th><th>Attempts</th><th>Usage</th><th>Created</th></tr></thead><tbody>{visible.length ? visible.map((run) => <tr key={run.id} onClick={() => void inspect(run)}><td><div className="run-cell"><div className="mini-run-icon"><PlayCircle size={15} /></div><div><strong>{shortId(run.id)}</strong><span>{run.input.slice(0, 70)}</span></div></div></td><td><strong className="table-strong">{run.agent_name}</strong><span className="table-sub">{run.environment}</span></td><td><StatusPill status={run.status} /></td><td><span className="icon-value"><GitBranch size={14} />{run.attempt_count ?? '—'}</span></td><td><span className="table-time">{run.usage ? `$${run.usage.cost.toFixed(4)}` : '—'}</span></td><td><span className="table-time">{formatRelative(run.created_at)}</span></td></tr>) : <tr><td colSpan={6} className="empty-cell">No runs match your search.</td></tr>}</tbody></table></div>
    </div>

    {selected && <div className="drawer-backdrop" onMouseDown={() => setSelected(null)}><aside className="run-drawer" onMouseDown={(e) => e.stopPropagation()}><div className="drawer-heading"><div><span className="page-eyebrow">RUN INSPECTOR</span><h3>{shortId(selected.id)}</h3></div><button className="icon-button" onClick={() => setSelected(null)}><X size={18} /></button></div><div className="drawer-status"><StatusPill status={selected.status} /><span>{selected.agent_name}</span></div><div className="drawer-request"><span>INPUT</span><p>{selected.input}</p></div><div className="run-detail-grid"><div><Clock3 size={15} /><span>Attempts</span><strong>{selected.attempts?.length ?? 1}</strong></div><div><Activity size={15} /><span>Events</span><strong>{events.length}</strong></div><div><CircleDollarSign size={15} /><span>Cost</span><strong>${selected.usage?.cost.toFixed(4) ?? '0.0000'}</strong></div><div><CalendarClock size={15} /><span>Created</span><strong>{formatRelative(selected.created_at)}</strong></div></div><div className="plan-pin"><span>PINNED EXECUTION PLAN</span><code>{selected.resolved_plan_id}</code></div>{selected.output && <div className="drawer-output"><span>OUTPUT</span><p>{selected.output}</p></div>}<div className="drawer-timeline"><h4>Event timeline</h4><EventTimeline events={events} /></div></aside></div>}
  </div>
}
