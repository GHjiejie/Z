import {
  Activity,
  Bot,
  Boxes,
  CheckCircle2,
  Clock3,
  FileText,
  GitBranch,
  LoaderCircle,
  PauseCircle,
  Plus,
  RefreshCw,
  Send,
  Sparkles,
  Square,
  Terminal,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { EventTimeline } from '../components/EventTimeline'
import { ErrorBanner, PageHeader, StatusPill, shortId } from '../components/UI'
import { api, streamUrl } from '../lib/api'
import type { Deployment, Run, RunArtifact, RuntimeEvent, ThreadSummary } from '../types'

const EVENT_TYPES = [
  'run.created', 'run.queued', 'run.preparing', 'run.started', 'run.resumed', 'run.completed', 'run.failed', 'run.cancelled', 'run.waiting_for_input', 'run.input_received',
  'model.started', 'model.delta', 'model.completed', 'tool.requested', 'tool.started', 'tool.completed', 'tool.failed', 'tool.approval_required',
  'subagent.started', 'subagent.progress', 'subagent.completed', 'todo.updated', 'interrupt.created', 'interrupt.resolved', 'artifact.created', 'usage.updated', 'skill.loaded',
]
type InspectorTab = 'plan' | 'events' | 'state' | 'artifacts' | 'usage'
type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'ended' | 'disconnected'

export function PlaygroundPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [deploymentId, setDeploymentId] = useState('')
  const [threadId, setThreadId] = useState('')
  const [input, setInput] = useState('Review the current release plan, identify risks, and prepare an auditable recommendation.')
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('plan')
  const [connection, setConnection] = useState<ConnectionState>('idle')
  const streamRef = useRef<EventSource | null>(null)

  const loadThreads = async () => setThreads((await api.threads()).items)
  useEffect(() => {
    Promise.all([api.deployments(), api.threads()]).then(([deploymentResult, threadResult]) => {
      setDeployments(deploymentResult.items); setThreads(threadResult.items)
      if (deploymentResult.items[0]) setDeploymentId(deploymentResult.items[0].id)
    }).catch((nextError) => setError(nextError.message))
    return () => streamRef.current?.close()
  }, [])

  const refreshRun = async (runId: string) => {
    const [nextRun, nextArtifacts] = await Promise.all([api.run(runId), api.runArtifacts(runId)])
    setRun(nextRun); setArtifacts(nextArtifacts.items)
    if (['SUCCEEDED', 'FAILED', 'CANCELLED', 'WAITING_FOR_APPROVAL', 'WAITING_FOR_INPUT'].includes(nextRun.status)) { setBusy(false); setConnection('ended') }
  }

  const openStream = (runId: string, after = 0, reconnecting = false) => {
    streamRef.current?.close(); setConnection(reconnecting ? 'reconnecting' : 'connecting')
    const source = new EventSource(streamUrl(runId, after)); streamRef.current = source
    source.onopen = () => setConnection('connected')
    const onEvent = (message: MessageEvent) => {
      const event = JSON.parse(message.data) as RuntimeEvent
      setEvents((previous) => previous.some((item) => item.event_id === event.event_id) ? previous : [...previous, event])
      if (['run.completed', 'run.failed', 'run.cancelled', 'tool.approval_required'].includes(event.type)) void refreshRun(runId)
    }
    EVENT_TYPES.forEach((type) => source.addEventListener(type, onEvent as EventListener))
    source.addEventListener('stream.idle', () => { source.close(); setConnection('ended'); void refreshRun(runId) })
    source.onerror = () => { source.close(); setConnection('disconnected'); void refreshRun(runId) }
  }

  const execute = async () => {
    if (!input.trim() || !deploymentId) return
    const currentRun = run
    setBusy(true); setError(''); setEvents([]); setArtifacts([]); setRun(null)
    try {
      let activeThreadId = threadId
      if (!activeThreadId) {
        const thread = await api.createThread(deploymentId, input.slice(0, 80)); activeThreadId = thread.id; setThreadId(thread.id)
      }
      const nextRun = currentRun?.status === 'WAITING_FOR_INPUT'
        ? await api.provideRunInput(currentRun.id, input)
        : await api.createRun(activeThreadId, input)
      setRun(nextRun); openStream(nextRun.id); await loadThreads()
    } catch (nextError) { setError((nextError as Error).message); setBusy(false); setConnection('disconnected') }
  }

  const chooseThread = async (id: string) => {
    setThreadId(id); setRun(null); setEvents([]); setArtifacts([]); setConnection('idle')
    if (!id) return
    const thread = await api.thread(id); setDeploymentId(thread.agent_deployment_id)
    const latest = thread.runs?.[0]
    if (latest) { const [detail, nextEvents, nextArtifacts] = await Promise.all([api.run(latest.id), api.runEvents(latest.id), api.runArtifacts(latest.id)]); setRun(detail); setEvents(nextEvents.items); setArtifacts(nextArtifacts.items); setConnection('ended') }
  }

  const startNewThread = () => { streamRef.current?.close(); setThreadId(''); setRun(null); setEvents([]); setArtifacts([]); setConnection('idle') }
  const cancel = async () => { if (run) { try { setRun(await api.cancelRun(run.id)); streamRef.current?.close(); setBusy(false); setConnection('ended') } catch (nextError) { setError((nextError as Error).message) } } }
  const modelMessages = events.filter((event) => event.type === 'model.delta' || event.type === 'model.completed')
  const todoEvent = [...events].reverse().find((event) => event.type === 'todo.updated')
  const subagentEvents = events.filter((event) => event.type.startsWith('subagent.'))

  return <div className="page-stack playground-page">
    <PageHeader eyebrow="RUNTIME PLANE" title="Test and run an agent" description="Start a new thread or continue an existing one, then inspect plans, events, state, artifacts, usage, and approval interrupts." actions={<button className="button secondary" onClick={startNewThread}><Plus size={16} /> New thread</button>} />
    {error && <ErrorBanner message={error} />}
    <div className="thread-toolbar panel"><label><span>Thread</span><select value={threadId} onChange={(event) => void chooseThread(event.target.value)}><option value="">New thread</option>{threads.map((thread) => <option value={thread.id} key={thread.id}>{thread.title} · {thread.agent_name ?? thread.deployment_name}</option>)}</select></label><div className={`connection-state state-${connection}`}><i /> Stream {connection}</div>{run && <Link className="button ghost" to={`/runs/${run.id}`}>Open full trace</Link>}</div>
    <div className="playground-shell">
      <section className="conversation-panel panel">
        <div className="conversation-toolbar"><div className="deployment-select-wrap"><div className="agent-logo"><Sparkles size={16} /></div><select aria-label="Agent deployment" value={deploymentId} onChange={(event) => setDeploymentId(event.target.value)} disabled={busy || !!threadId}>{deployments.map((deployment) => <option value={deployment.id} key={deployment.id}>{deployment.agent_name} · {deployment.environment}</option>)}</select></div><span className="thread-identity">{threadId ? `Continuing ${shortId(threadId)}` : 'New thread'}</span></div>
        <div className="conversation-body">
          {!run ? <div className="welcome-message"><div className="welcome-orb"><Sparkles size={28} /></div><span className="page-eyebrow">NEW EXECUTION</span><h3>What should the agent work on?</h3><p>This first message creates a thread. Later messages continue the same thread until you choose New thread.</p><div className="prompt-suggestions"><button onClick={() => setInput('Review the current release plan, identify risks, and prepare an auditable recommendation.')}>Review a release plan</button><button onClick={() => setInput('Deploy the approved service update to production and write a release artifact.')}>Request a production deployment</button><button onClick={() => setInput('Research the architecture constraints and summarize an implementation strategy.')}>Delegate research</button></div></div> : <>
            <div className="chat-message user-message"><div className="message-avatar user">YOU</div><div><span>You <small>{new Date(run.created_at).toLocaleTimeString()}</small></span><p>{run.input}</p></div></div>
            <div className="chat-message assistant-message"><div className="message-avatar agent"><Bot size={16} /></div><div className="assistant-bubble"><span>DeepAgent <small>{run.status === 'SUCCEEDED' ? 'completed' : run.status.replaceAll('_', ' ').toLowerCase()}</small></span>{modelMessages.length ? modelMessages.map((event) => <p key={event.event_id}>{event.payload.delta ?? event.payload.output}</p>) : !['WAITING_FOR_APPROVAL', 'WAITING_FOR_INPUT', 'CANCELLED'].includes(run.status) && <div className="agent-thinking"><span /><span /><span /> Preparing execution context</div>}{run.output && !modelMessages.some((event) => event.type === 'model.completed') && <p>{run.output}</p>}{run.status === 'WAITING_FOR_APPROVAL' && <div className="inline-approval"><PauseCircle size={17} /><div><strong>Human approval required</strong><span>The run is checkpointed and can wait safely.</span></div><Link to="/approvals">Review</Link></div>}{run.status === 'WAITING_FOR_INPUT' && <div className="inline-approval input-request"><PauseCircle size={17} /><div><strong>Reviewer requested changes</strong><span>Enter revised instructions below to resume this same run in a new attempt.</span></div></div>}</div></div>
          </>}
        </div>
        <div className="composer-wrap"><div className="composer"><textarea rows={3} value={input} disabled={busy} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) void execute() }} placeholder={run?.status === 'WAITING_FOR_INPUT' ? 'Describe the requested changes to resume this run…' : 'Give your agent a task…'} /><div className="composer-footer"><span>⌘ Enter to run</span>{busy ? <button className="stop-button" onClick={() => void cancel()}><Square size={13} fill="currentColor" /> Stop</button> : <button className="send-button" disabled={!input.trim() || !deploymentId} onClick={() => void execute()}><Send size={15} />{run?.status === 'WAITING_FOR_INPUT' ? 'Resume run' : threadId ? 'Continue thread' : 'Start run'}</button>}</div></div></div>
      </section>

      <aside className="inspector-panel panel"><div className="inspector-tabs" role="tablist">{(['plan', 'events', 'state', 'artifacts', 'usage'] as InspectorTab[]).map((item) => <button role="tab" aria-selected={inspectorTab === item} className={inspectorTab === item ? 'active' : ''} onClick={() => setInspectorTab(item)} key={item}>{item === 'plan' ? <CheckCircle2 size={14} /> : item === 'events' ? <Activity size={14} /> : item === 'state' ? <Boxes size={14} /> : item === 'artifacts' ? <FileText size={14} /> : <Clock3 size={14} />}{item}{item === 'artifacts' && artifacts.length > 0 && <i>{artifacts.length}</i>}</button>)}</div><div className="inspector-content">
        {run && <div className="run-summary-card"><div><span>RUN</span><code>{shortId(run.id)}</code></div><StatusPill status={run.status} /><div className="run-summary-meta"><span><GitBranch size={13} />{shortId(run.resolved_plan_id, 6)}</span><span><Clock3 size={13} />Attempt {run.attempts?.length ?? 1}</span></div></div>}
        {inspectorTab === 'plan' && <>{todoEvent && <div className="todo-card"><div className="inspector-section-title"><CheckCircle2 size={15} /><strong>Agent plan</strong><span>{todoEvent.payload.items.filter((item: any) => item.status === 'completed').length}/{todoEvent.payload.items.length}</span></div>{todoEvent.payload.items.map((item: any) => <div className={`todo-item ${item.status}`} key={item.id}><span>{item.status === 'completed' ? <CheckCircle2 size={14} /> : item.status === 'in_progress' ? <LoaderCircle className="spin" size={14} /> : <Clock3 size={14} />}</span><p>{item.title}</p></div>)}</div>}{subagentEvents.length > 0 && <div className="subagent-card"><div className="inspector-section-title"><Bot size={15} /><strong>SubAgent tree</strong><span>{new Set(subagentEvents.map((event) => event.execution_path.join('/'))).size} child</span></div><div className="subagent-node"><div className="tree-line" /><div className="agent-logo tiny"><Bot size={13} /></div><div><strong>{String(subagentEvents[0]?.payload.name ?? 'researcher')}</strong><span>{subagentEvents.at(-1)?.type === 'subagent.completed' ? 'Completed' : 'Working'}</span></div></div></div>}{!todoEvent && !subagentEvents.length && <InspectorEmpty icon={CheckCircle2} title="No plan yet" text="Start a run to inspect plan and SubAgent progress." />}</>}
        {inspectorTab === 'events' && (events.length ? <EventTimeline events={events} compact /> : <InspectorEmpty icon={Terminal} title="No events yet" text="Runtime events appear here as the run progresses." />)}
        {inspectorTab === 'state' && <pre className="state-view">{JSON.stringify(run ? { status: run.status, checkpoint: run.checkpoint, metadata: run.metadata, attempts: run.attempts } : {}, null, 2)}</pre>}
        {inspectorTab === 'artifacts' && (artifacts.length ? artifacts.map((artifact) => <a className="artifact-card" href={artifact.uri} target="_blank" rel="noreferrer" key={artifact.id}><div className="artifact-icon"><FileText size={18} /></div><div><strong>{artifact.name}</strong><span>{artifact.media_type} · {artifact.size_bytes} bytes</span><code>{artifact.content_hash.slice(0, 18)}…</code></div></a>) : <InspectorEmpty icon={FileText} title="No artifacts yet" text="Run outputs appear here with their content hash." />)}
        {inspectorTab === 'usage' && (run?.usage ? <div className="usage-detail compact"><div><span>Input tokens</span><strong>{run.usage.input_tokens}</strong></div><div><span>Output tokens</span><strong>{run.usage.output_tokens}</strong></div><div><span>Model calls</span><strong>{run.usage.model_calls}</strong></div><div><span>Tool calls</span><strong>{run.usage.tool_calls}</strong></div><div><span>SubAgent calls</span><strong>{run.usage.subagent_calls}</strong></div><div><span>Cost</span><strong>${run.usage.cost.toFixed(4)}</strong></div></div> : <InspectorEmpty icon={Clock3} title="No usage yet" text="Usage appears after the runtime reports it." />)}
      </div>{connection === 'disconnected' && run && <div className="inspector-reconnect"><span>Live updates disconnected.</span><button className="button secondary" onClick={() => openStream(run.id, events.at(-1)?.sequence ?? 0, true)}><RefreshCw size={14} /> Reconnect</button></div>}</aside>
    </div>
  </div>
}

function InspectorEmpty({ icon: Icon, title, text }: { icon: typeof Terminal; title: string; text: string }) {
  return <div className="inspector-empty"><Icon size={24} /><h4>{title}</h4><p>{text}</p></div>
}
