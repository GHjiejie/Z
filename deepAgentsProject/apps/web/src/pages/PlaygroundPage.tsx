import {
  Activity,
  Bot,
  Boxes,
  CheckCircle2,
  ChevronDown,
  Clock3,
  Code2,
  FileText,
  GitBranch,
  Hammer,
  LoaderCircle,
  Maximize2,
  Paperclip,
  PauseCircle,
  Play,
  Send,
  Sparkles,
  Square,
  Terminal,
  Wrench,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { EventTimeline } from '../components/EventTimeline'
import { ErrorBanner, PageHeader, StatusPill, shortId } from '../components/UI'
import { api, streamUrl } from '../lib/api'
import type { Deployment, Run, RuntimeEvent } from '../types'

const EVENT_TYPES = [
  'run.created', 'run.queued', 'run.preparing', 'run.started', 'run.resumed', 'run.completed', 'run.failed', 'run.cancelled',
  'model.started', 'model.delta', 'model.completed', 'tool.requested', 'tool.started', 'tool.completed', 'tool.failed', 'tool.approval_required',
  'subagent.started', 'subagent.progress', 'subagent.completed', 'todo.updated', 'interrupt.created', 'interrupt.resolved', 'artifact.created', 'usage.updated',
]

export function PlaygroundPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [deploymentId, setDeploymentId] = useState('')
  const [input, setInput] = useState('Review the current release plan, identify risks, and prepare an auditable recommendation.')
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [artifacts, setArtifacts] = useState<Array<Record<string, any>>>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [traceTab, setTraceTab] = useState<'trace' | 'state' | 'artifacts'>('trace')
  const streamRef = useRef<EventSource | null>(null)

  useEffect(() => {
    api.deployments().then(({ items }) => { setDeployments(items); if (items[0]) setDeploymentId(items[0].id) }).catch((err) => setError(err.message))
    return () => streamRef.current?.close()
  }, [])

  const refreshRun = async (runId: string) => {
    const [nextRun, nextArtifacts] = await Promise.all([api.run(runId), api.runArtifacts(runId)])
    setRun(nextRun); setArtifacts(nextArtifacts.items)
    if (['SUCCEEDED', 'FAILED', 'CANCELLED', 'WAITING_FOR_APPROVAL'].includes(nextRun.status)) setBusy(false)
  }

  const openStream = (runId: string, after = 0) => {
    streamRef.current?.close()
    const source = new EventSource(streamUrl(runId, after))
    streamRef.current = source
    const onEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RuntimeEvent
      setEvents((previous) => previous.some((item) => item.event_id === event.event_id) ? previous : [...previous, event])
      if (['run.completed', 'run.failed', 'run.cancelled', 'tool.approval_required'].includes(event.type)) void refreshRun(runId)
    }
    EVENT_TYPES.forEach((type) => source.addEventListener(type, onEvent as EventListener))
    source.addEventListener('stream.idle', () => { source.close(); void refreshRun(runId) })
    source.onerror = () => { source.close(); void refreshRun(runId) }
  }

  const execute = async () => {
    if (!input.trim() || !deploymentId) return
    setBusy(true); setError(''); setEvents([]); setArtifacts([]); setRun(null)
    try {
      const thread = await api.createThread(deploymentId, input.slice(0, 80))
      const nextRun = await api.createRun(thread.id, input)
      setRun(nextRun); openStream(nextRun.id)
    } catch (err) { setError((err as Error).message); setBusy(false) }
  }

  const cancel = async () => {
    if (!run) return
    try { setRun(await api.cancelRun(run.id)); streamRef.current?.close(); setBusy(false) } catch (err) { setError((err as Error).message) }
  }

  const modelMessages = events.filter((event) => event.type === 'model.delta' || event.type === 'model.completed')
  const todoEvent = [...events].reverse().find((event) => event.type === 'todo.updated')
  const subagentEvents = events.filter((event) => event.type.startsWith('subagent.'))

  return (
    <div className="page-stack playground-page">
      <PageHeader eyebrow="RUNTIME PLANE" title="Run an agent with full visibility" description="Messages are only one projection. Inspect plans, tools, SubAgents, approval interrupts, artifacts, and usage as separate runtime events." />
      {error && <ErrorBanner message={error} />}
      <div className="playground-shell">
        <section className="conversation-panel panel">
          <div className="conversation-toolbar">
            <div className="deployment-select-wrap"><div className="agent-logo"><Sparkles size={16} /></div><select aria-label="Agent deployment" value={deploymentId} onChange={(e) => setDeploymentId(e.target.value)} disabled={busy}>{deployments.map((deployment) => <option value={deployment.id} key={deployment.id}>{deployment.agent_name} · {deployment.environment}</option>)}</select><ChevronDown size={15} /></div>
            <div className="conversation-tools"><span className="live-connection"><i /> SSE connected</span><button className="icon-button"><Maximize2 size={16} /></button></div>
          </div>
          <div className="conversation-body">
            {!run ? <div className="welcome-message"><div className="welcome-orb"><Sparkles size={28} /></div><span className="page-eyebrow">DEEP AGENTS HARNESS</span><h3>What should the agent work on?</h3><p>Try an analytical task, or include “deploy to production” to exercise the human approval and checkpoint-resume path.</p><div className="prompt-suggestions"><button onClick={() => setInput('Review the current release plan, identify risks, and prepare an auditable recommendation.')}>Review a release plan</button><button onClick={() => setInput('Deploy the approved service update to production and write a release artifact.')}>Trigger production approval</button><button onClick={() => setInput('Research the architecture constraints and summarize an implementation strategy.')}>Delegate research</button></div></div> : <>
              <div className="chat-message user-message"><div className="message-avatar user">ZJ</div><div><span>You <small>just now</small></span><p>{run.input}</p></div></div>
              <div className="chat-message assistant-message"><div className="message-avatar agent"><Bot size={16} /></div><div className="assistant-bubble"><span>DeepAgent <small>{run.status === 'SUCCEEDED' ? 'completed' : 'working'}</small></span>
                {modelMessages.length ? modelMessages.map((event) => <p key={event.event_id}>{event.payload.delta ?? event.payload.output}</p>) : <div className="agent-thinking"><span /><span /><span /> Preparing execution context</div>}
                {run.output && !modelMessages.some((event) => event.type === 'model.completed') && <p>{run.output}</p>}
                {run.status === 'WAITING_FOR_APPROVAL' && <div className="inline-approval"><PauseCircle size={17} /><div><strong>Human approval required</strong><span>The run is checkpointed and can safely wait.</span></div><Link to="/approvals">Review</Link></div>}
              </div></div>
            </>}
          </div>
          <div className="composer-wrap">
            <div className="composer"><textarea rows={3} value={input} disabled={busy} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) void execute() }} placeholder="Give your agent a task…" /><div className="composer-footer"><div><button><Paperclip size={16} /></button><button><Code2 size={16} /></button><span>⌘ Enter to run</span></div>{busy ? <button className="stop-button" onClick={() => void cancel()}><Square size={13} fill="currentColor" /> Stop</button> : <button className="send-button" disabled={!input.trim() || !deploymentId} onClick={() => void execute()}><Send size={15} /> Run agent</button>}</div></div>
          </div>
        </section>

        <aside className="inspector-panel panel">
          <div className="inspector-tabs"><button className={traceTab === 'trace' ? 'active' : ''} onClick={() => setTraceTab('trace')}><Activity size={14} /> Trace</button><button className={traceTab === 'state' ? 'active' : ''} onClick={() => setTraceTab('state')}><Boxes size={14} /> State</button><button className={traceTab === 'artifacts' ? 'active' : ''} onClick={() => setTraceTab('artifacts')}><FileText size={14} /> Artifacts {artifacts.length > 0 && <i>{artifacts.length}</i>}</button></div>
          <div className="inspector-content">
            {run && <div className="run-summary-card"><div><span>RUN</span><code>{shortId(run.id)}</code></div><StatusPill status={run.status} /><div className="run-summary-meta"><span><GitBranch size={13} /> {shortId(run.resolved_plan_id, 6)}</span><span><Clock3 size={13} /> Attempt {run.attempts?.length ?? 1}</span></div></div>}
            {traceTab === 'trace' && <>
              {todoEvent && <div className="todo-card"><div className="inspector-section-title"><CheckCircle2 size={15} /><strong>Agent plan</strong><span>{todoEvent.payload.items.filter((item: any) => item.status === 'completed').length}/{todoEvent.payload.items.length}</span></div>{todoEvent.payload.items.map((item: any) => <div className={`todo-item ${item.status}`} key={item.id}><span>{item.status === 'completed' ? <CheckCircle2 size={14} /> : item.status === 'in_progress' ? <LoaderCircle className="spin" size={14} /> : <Clock3 size={14} />}</span><p>{item.title}</p></div>)}</div>}
              {subagentEvents.length > 0 && <div className="subagent-card"><div className="inspector-section-title"><Bot size={15} /><strong>SubAgent tree</strong><span>1 child</span></div><div className="subagent-node"><div className="tree-line" /><div className="agent-logo tiny"><Bot size={13} /></div><div><strong>researcher</strong><span>{subagentEvents.at(-1)?.type === 'subagent.completed' ? 'Completed' : 'Working'}</span></div></div></div>}
              <div className="inspector-section-title timeline-title"><Activity size={15} /><strong>Runtime events</strong><span>{events.length}</span></div>
              {events.length ? <EventTimeline events={events} compact /> : <div className="inspector-empty"><Terminal size={24} /><h4>No events yet</h4><p>Start a run to watch the execution timeline.</p></div>}
            </>}
            {traceTab === 'state' && <pre className="state-view">{JSON.stringify(run ? { status: run.status, checkpoint: run.checkpoint, usage: run.usage, attempts: run.attempts } : {}, null, 2)}</pre>}
            {traceTab === 'artifacts' && <>{artifacts.length ? artifacts.map((artifact) => <div className="artifact-card" key={artifact.id}><div className="artifact-icon"><FileText size={18} /></div><div><strong>{artifact.name}</strong><span>{artifact.media_type} · {artifact.size_bytes} bytes</span><code>{artifact.content_hash.slice(0, 14)}…</code></div></div>) : <div className="inspector-empty"><FileText size={24} /><h4>No artifacts yet</h4><p>Run outputs appear here with their content hash.</p></div>}</>}
          </div>
        </aside>
      </div>
    </div>
  )
}

