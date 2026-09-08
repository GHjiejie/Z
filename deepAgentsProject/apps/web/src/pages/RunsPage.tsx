import {
  Activity,
  CalendarClock,
  CircleDollarSign,
  Clock3,
  Copy,
  FileText,
  GitBranch,
  PlayCircle,
  RefreshCw,
  RotateCcw,
  Search,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { EventTimeline, eventCategory } from '../components/EventTimeline'
import { ThreadSharing } from '../components/ThreadSharing'
import { CancellationNotice } from '../components/CancellationNotice'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'
import { api } from '../lib/api'
import type { Run, RunArtifact, RuntimeEvent } from '../types'

type DetailTab = 'summary' | 'timeline' | 'attempts' | 'state' | 'artifacts' | 'usage'
const eventFilters = ['All', 'Runtime', 'Model', 'Tool', 'SubAgent', 'Approval', 'Artifact', 'Error']

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [listLoading, setListLoading] = useState(false)
  const requestEpoch = useRef(0)
  const [selected, setSelected] = useState<Run | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([])
  const [loaded, setLoaded] = useState(false)
  const [detailLoading, setDetailLoading] = useState(false)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<DetailTab>('summary')
  const [eventFilter, setEventFilter] = useState('All')
  const [searchParams, setSearchParams] = useSearchParams()
  const { runId } = useParams()
  const navigate = useNavigate()

  const search = searchParams.get('q') ?? ''
  const status = searchParams.get('status') ?? 'ALL'
  const load = async (cursor?: string) => {
    const epoch = ++requestEpoch.current
    setListLoading(true)
    setError('')
    if (!cursor) { setNextCursor(null); setRuns([]) }
    try {
      const result = await api.runs({ cursor, limit: 50, q: search, status: status === 'ALL' ? undefined : status })
      if (epoch !== requestEpoch.current) return
      setRuns((previous) => cursor ? [...previous, ...result.items.filter((item) => !previous.some((old) => old.id === item.id))] : result.items)
      setNextCursor(result.next_cursor)
    } catch (nextError) {
      if (epoch === requestEpoch.current) setError((nextError as Error).message)
    } finally {
      if (epoch === requestEpoch.current) { setLoaded(true); setListLoading(false) }
    }
  }

  const inspect = async (id: string) => {
    setDetailLoading(true); setError('')
    try {
      const [run, nextEvents, nextArtifacts] = await Promise.all([api.run(id), api.runEvents(id), api.runArtifacts(id)])
      setSelected(run); setEvents(nextEvents.items); setArtifacts(nextArtifacts.items)
    } catch (nextError) { setSelected(null); setEvents([]); setArtifacts([]); setError((nextError as Error).message) } finally { setDetailLoading(false) }
  }

  useEffect(() => {
    ++requestEpoch.current
    const timeout = window.setTimeout(() => void load(), 200)
    return () => { window.clearTimeout(timeout); ++requestEpoch.current }
  }, [search, status])
  useEffect(() => { if (runId) void inspect(runId); else { setSelected(null); setEvents([]); setArtifacts([]) } }, [runId])

  const environment = searchParams.get('environment') ?? 'ALL'
  const statuses = ['CREATED', 'QUEUED', 'PREPARING', 'RUNNING', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_INPUT', 'PAUSED', 'ORPHANED', 'RESUMING', 'CANCELLING', 'CANCELLED', 'TIMED_OUT', 'FAILED', 'FAILED_BUDGET', 'SUCCEEDED']
  const environments = useMemo(() => [...new Set(runs.map((run) => run.environment).filter(Boolean))].sort() as string[], [runs])
  const visible = runs.filter((run) => {
    const matchesSearch = `${run.id} ${run.thread_id} ${run.agent_name} ${run.input} ${run.status}`.toLowerCase().includes(search.toLowerCase())
    return matchesSearch && (environment === 'ALL' || run.environment === environment)
  })
  const setFilter = (key: string, value: string) => { const next = new URLSearchParams(searchParams); if (!value || value === 'ALL') next.delete(key); else next.set(key, value); setSearchParams(next, { replace: true }) }
  const filteredEvents = eventFilter === 'All' ? events : events.filter((event) => eventCategory(event) === eventFilter)

  if (!loaded) return <LoadingBlock />

  return <div className="page-stack runs-page">
    <PageHeader eyebrow="OBSERVABILITY" title="Runs and traces" description="Filter execution history, open a shareable run URL, and inspect attempts, state, artifacts, usage, and raw events." actions={<button className="button secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</button>} />
    {error && <ErrorBanner message={error} />}
    {(nextCursor || listLoading) && <div className="table-toolbar"><span>{runs.length} loaded</span><button className="button secondary" disabled={listLoading || !nextCursor} onClick={() => nextCursor && void load(nextCursor)}>{listLoading ? 'Loading…' : 'Load older runs'}</button></div>}
    <div className="panel runs-panel">
      <div className="table-toolbar runs-toolbar"><label className="table-search"><Search size={16} /><input aria-label="Search runs" value={search} onChange={(event) => setFilter('q', event.target.value)} placeholder="Search run, thread, agent, or input…" /></label><label className="filter-select"><span>Status</span><select value={status} onChange={(event) => setFilter('status', event.target.value)}><option value="ALL">All statuses</option>{statuses.map((item) => <option value={item} key={item}>{item.replaceAll('_', ' ')}</option>)}</select></label><label className="filter-select"><span>Environment</span><select value={environment} onChange={(event) => setFilter('environment', event.target.value)}><option value="ALL">All environments</option>{environments.map((item) => <option value={item} key={item}>{item}</option>)}</select></label><span>{visible.length} runs</span></div>
      <div className="table-wrap"><table className="data-table runs-table"><thead><tr><th>Run & request</th><th>Agent</th><th>Status</th><th>Attempts</th><th>Usage</th><th>Created</th></tr></thead><tbody>{visible.length ? visible.map((run) => <tr key={run.id}><td><Link className="run-cell" to={`/advanced/runs/${run.id}?${searchParams.toString()}`}><div className="mini-run-icon"><PlayCircle size={15} /></div><div><strong>{shortId(run.id)}</strong><span>{run.input.slice(0, 70)}</span></div></Link></td><td><strong className="table-strong">{run.agent_name}</strong><span className="table-sub">{run.environment}</span></td><td><StatusPill status={run.status} /></td><td><span className="icon-value"><GitBranch size={14} />{run.attempt_count ?? '—'}</span></td><td><span className="table-time">{run.usage ? `${run.usage.cost.toFixed(4)}${run.usage.unsettled_model_calls ? ' (pending)' : ''}` : '—'}</span></td><td><time className="table-time" dateTime={run.created_at} title={new Date(run.created_at).toLocaleString()}>{formatRelative(run.created_at)}</time></td></tr>) : <tr><td colSpan={6} className="empty-cell">No runs match these filters.</td></tr>}</tbody></table></div>
    </div>

    {runId && <div className="run-detail-backdrop" onMouseDown={() => navigate(`/advanced/runs?${searchParams.toString()}`)}><aside className="run-detail" aria-label="Run inspector" onMouseDown={(event) => event.stopPropagation()}>{detailLoading && !selected ? <LoadingBlock label="Loading run trace…" /> : selected && <>
      <div className="drawer-heading"><div><span className="page-eyebrow">RUN INSPECTOR</span><h3>{shortId(selected.id)}</h3><span className="drawer-subtitle">Thread {shortId(selected.thread_id)}</span></div><div className="drawer-heading-actions"><button className="icon-button" aria-label="Copy run URL" onClick={() => navigator.clipboard.writeText(window.location.href)}><Copy size={17} /></button><button className="icon-button" aria-label="Close run inspector" onClick={() => navigate(`/advanced/runs?${searchParams.toString()}`)}><X size={18} /></button></div></div>
      <div className="drawer-status"><StatusPill status={selected.status} /><span>{selected.agent_name}</span><span>{selected.environment}</span>{['FAILED', 'ORPHANED', 'TIMED_OUT'].includes(selected.status) && <button className="button secondary" onClick={async () => { await api.retryRun(selected.id); await inspect(selected.id); await load() }}><RotateCcw size={14} /> Retry</button>}</div>
      <div className="run-detail-tabs" role="tablist">{(['summary', 'timeline', 'attempts', 'state', 'artifacts', 'usage'] as DetailTab[]).map((item) => <button role="tab" aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)} key={item}>{item}</button>)}</div>
      <div className="run-detail-content">
        {tab === 'summary' && <><RunSummary run={selected} events={events} /><ThreadSharing key={selected.thread_id} threadId={selected.thread_id} /></>}
        {tab === 'timeline' && <><div className="event-filters">{eventFilters.map((item) => <button className={eventFilter === item ? 'active' : ''} onClick={() => setEventFilter(item)} key={item}>{item}</button>)}</div>{filteredEvents.length ? <EventTimeline events={filteredEvents} /> : <div className="detail-empty">No events in this category.</div>}</>}
        {tab === 'attempts' && <div className="attempt-list">{selected.attempts?.map((attempt, index) => <div className="attempt-card" key={String(attempt.id)}><div><span>ATTEMPT {Number(attempt.attempt_number ?? index + 1)}</span><StatusPill status={String(attempt.status)} /></div><dl><div><dt>ID</dt><dd><code>{String(attempt.id)}</code></dd></div><div><dt>Worker</dt><dd>{String(attempt.worker_id ?? 'Not assigned')}</dd></div><div><dt>Created</dt><dd>{new Date(String(attempt.created_at)).toLocaleString()}</dd></div><div><dt>Updated</dt><dd>{new Date(String(attempt.updated_at)).toLocaleString()}</dd></div></dl></div>)}</div>}
        {tab === 'state' && <pre className="state-view">{JSON.stringify({ status: selected.status, checkpoint: selected.checkpoint, metadata: selected.metadata, current_attempt_id: selected.current_attempt_id }, null, 2)}</pre>}
        {tab === 'artifacts' && <div className="artifact-list">{artifacts.length ? artifacts.map((artifact) => <div className="artifact-card" key={artifact.id}><div className="artifact-icon"><FileText size={18} /></div><div><a href={artifact.uri} target="_blank" rel="noreferrer"><strong>{artifact.name}</strong></a><span>{artifact.media_type} · {artifact.size_bytes} bytes</span><code>{artifact.content_hash}</code></div><button aria-label={`Copy ${artifact.name} URI`} className="icon-button" onClick={() => navigator.clipboard.writeText(new URL(artifact.uri, window.location.origin).toString())}><Copy size={15} /></button></div>) : <div className="detail-empty">This run produced no artifacts.</div>}</div>}
        {tab === 'usage' && <div className="usage-detail">{selected.usage ? <><div><span>Input tokens</span><strong>{selected.usage.input_tokens}</strong></div><div><span>Output tokens</span><strong>{selected.usage.output_tokens}</strong></div><div><span>Model calls</span><strong>{selected.usage.model_calls}</strong></div><div><span>Tool calls</span><strong>{selected.usage.tool_calls}</strong></div><div><span>SubAgent calls</span><strong>{selected.usage.subagent_calls}</strong></div><div><span>{selected.usage.unsettled_model_calls ? 'Cost incl. pending charges' : 'Cost'}</span><strong>${selected.usage.cost.toFixed(4)}</strong></div></> : <div className="detail-empty">Usage has not been reported.</div>}</div>}
      </div>
    </>}</aside></div>}
  </div>
}

function RunSummary({ run, events }: { run: Run; events: RuntimeEvent[] }) {
  const failure = [...events].reverse().find((event) => event.type.includes('failed'))
  return <div className="run-summary-detail">
    <CancellationNotice run={run} />
    {failure && <div className="failure-summary"><strong>Failure summary</strong><span>{String(failure.payload.message ?? failure.payload.reason ?? 'The runtime reported a failure.')}</span><small>Last event sequence #{failure.sequence}</small></div>}
    <div className="drawer-request"><span>INPUT</span><p>{run.input}</p></div>{run.output && <div className="drawer-output"><span>OUTPUT</span><p>{run.output}</p></div>}
    <div className="run-detail-grid"><div><Clock3 size={15} /><span>Attempts</span><strong>{run.attempts?.length ?? 1}</strong></div><div><Activity size={15} /><span>Events</span><strong>{events.length}</strong></div><div><CircleDollarSign size={15} /><span>{run.usage?.unsettled_model_calls ? 'Cost incl. pending charges' : 'Cost'}</span><strong>${run.usage?.cost.toFixed(4) ?? '0.0000'}</strong></div><div><CalendarClock size={15} /><span>Created</span><strong title={new Date(run.created_at).toLocaleString()}>{formatRelative(run.created_at)}</strong></div></div>
    <div className="detail-identifiers"><div><span>RUN</span><code>{run.id}</code></div><div><span>THREAD</span><code>{run.thread_id}</code></div><div><span>PINNED EXECUTION PLAN</span><code>{run.resolved_plan_id}</code></div></div>
  </div>
}
