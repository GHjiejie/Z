import {
  Activity,
  ArrowUp,
  Bot,
  Boxes,
  Brain,
  CheckCircle2,
  ChevronDown,
  Clock3,
  FileText,
  FolderOpen,
  GitBranch,
  History,
  LoaderCircle,
  Menu,
  MessageSquareText,
  PauseCircle,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Square,
  Terminal,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { EventTimeline, eventCategoryKey, type EventCategory } from '../components/EventTimeline'
import { MarkdownContent } from '../components/MarkdownContent'
import { ErrorBanner, StatusPill, formatRelative, shortId } from '../components/UI'
import { usePlatform } from '../context/PlatformContext'
import { api, streamUrl } from '../lib/api'
import type { Deployment, IntentRoutingDecision, Repository, Run, RunArtifact, RuntimeEvent, ThreadSummary } from '../types'

const RUNNING_STATUSES = ['CREATED', 'QUEUED', 'PREPARING', 'RUNNING', 'RESUMING']
const AUTO_DEPLOYMENT = 'auto'

type InspectorTab = 'plan' | 'events' | 'state' | 'artifacts' | 'usage'
type ConnectionState = 'idle' | 'connecting' | 'connected' | 'reconnecting' | 'ended' | 'disconnected'

const EVENT_FILTERS: Array<{ key: 'all' | EventCategory; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'runtime', label: 'Runtime' },
  { key: 'graph', label: 'Graph' },
  { key: 'model', label: 'Model' },
  { key: 'tools', label: 'Tools' },
  { key: 'human', label: 'Human' },
  { key: 'subagents', label: 'SubAgents' },
  { key: 'output', label: 'Output' },
]

async function loadAllRunEvents(runId: string) {
  const loaded: RuntimeEvent[] = []
  let cursor = 0
  while (true) {
    const page = await api.runEvents(runId, cursor)
    loaded.push(...page.items)
    if (page.items.length < 500) return loaded
    cursor = page.items.at(-1)?.sequence ?? cursor
  }
}

export function PlaygroundPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [conversationRuns, setConversationRuns] = useState<Run[]>([])
  const [deploymentId, setDeploymentId] = useState(AUTO_DEPLOYMENT)
  const [threadId, setThreadId] = useState('')
  const [input, setInput] = useState('')
  const [run, setRun] = useState<Run | null>(null)
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([])
  const [busy, setBusy] = useState(false)
  const [loadingThread, setLoadingThread] = useState(false)
  const [error, setError] = useState('')
  const [threadQuery, setThreadQuery] = useState('')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(() => typeof window !== 'undefined' && window.matchMedia('(min-width: 1181px)').matches)
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('events')
  const [connection, setConnection] = useState<ConnectionState>('idle')
  const [pendingRoute, setPendingRoute] = useState<{ decision: IntentRoutingDecision; message: string } | null>(null)
  const [routeOverrideId, setRouteOverrideId] = useState('')
  const [routeRepositoryId, setRouteRepositoryId] = useState('')
  const [routeBaseRef, setRouteBaseRef] = useState('main')
  const [routingNotice, setRoutingNotice] = useState<IntentRoutingDecision | null>(null)
  const streamRef = useRef<EventSource | null>(null)
  const chatScrollRef = useRef<HTMLDivElement | null>(null)
  const { context } = usePlatform()

  const loadThreads = async () => {
    const result = await api.threads()
    setThreads(result.items)
    return result.items
  }

  useEffect(() => {
    Promise.all([api.deployments(), api.threads(), api.repositories()]).then(([deploymentResult, threadResult, repositoryResult]) => {
      const environment = context?.environment.id.replace(/^env_/, '')
      setDeployments(deploymentResult.items.filter((item) => item.status === 'ACTIVE' && (!environment || item.environment === environment)))
      setThreads(threadResult.items)
      setRepositories(repositoryResult.items.filter((item) => item.status === 'ACTIVE'))
      setDeploymentId(AUTO_DEPLOYMENT)
      if (repositoryResult.items[0]) {
        setRouteRepositoryId(repositoryResult.items[0].id)
        setRouteBaseRef(repositoryResult.items[0].default_branch)
      }
    }).catch((nextError) => setError(nextError.message))
    return () => streamRef.current?.close()
  }, [context?.environment.id])

  useEffect(() => {
    const scroller = chatScrollRef.current
    scroller?.scrollTo({ top: scroller.scrollHeight, behavior: 'smooth' })
  }, [conversationRuns, events.length, run?.status])

  const updateConversationRun = (nextRun: Run) => {
    setConversationRuns((previous) => {
      const exists = previous.some((item) => item.id === nextRun.id)
      return exists ? previous.map((item) => item.id === nextRun.id ? nextRun : item) : [...previous, nextRun]
    })
  }

  const refreshRun = async (runId: string) => {
    const [nextRun, nextArtifacts] = await Promise.all([api.run(runId), api.runArtifacts(runId)])
    setRun(nextRun)
    setArtifacts(nextArtifacts.items)
    updateConversationRun(nextRun)
    if (!RUNNING_STATUSES.includes(nextRun.status)) {
      setBusy(false)
      setConnection('ended')
    }
  }

  const openStream = (runId: string, after = 0, reconnecting = false) => {
    streamRef.current?.close()
    setConnection(reconnecting ? 'reconnecting' : 'connecting')
    const source = new EventSource(streamUrl(runId, after))
    streamRef.current = source
    source.onopen = () => setConnection('connected')
    const onEvent = (message: MessageEvent) => {
      try {
        const event = JSON.parse(message.data) as RuntimeEvent
        setEvents((previous) => previous.some((item) => item.event_id === event.event_id)
          ? previous
          : [...previous, event].sort((left, right) => left.sequence - right.sequence))
        if (['run.completed', 'run.failed', 'run.cancelled', 'run.waiting_for_input', 'tool.approval_required'].includes(event.type)) void refreshRun(runId)
      } catch {
        setError('The live event stream returned an invalid event payload.')
      }
    }
    source.addEventListener('runtime.event', onEvent as EventListener)
    source.addEventListener('stream.idle', () => {
      source.close()
      setConnection('ended')
      void refreshRun(runId)
    })
    source.onerror = () => {
      source.close()
      setConnection('disconnected')
      void refreshRun(runId)
    }
  }

  const chooseThread = async (id: string) => {
    streamRef.current?.close()
    setThreadId(id)
    setSidebarOpen(false)
    setRun(null)
    setConversationRuns([])
    setEvents([])
    setArtifacts([])
    setInput('')
    setConnection('idle')
    setPendingRoute(null)
    setRoutingNotice(null)
    if (!id) return
    setLoadingThread(true)
    setError('')
    try {
      const thread = await api.thread(id)
      setDeploymentId(thread.agent_deployment_id)
      const orderedRuns = [...(thread.runs ?? [])].reverse()
      setConversationRuns(orderedRuns)
      const latest = orderedRuns.at(-1)
      if (latest) {
        const [detail, nextEvents, nextArtifacts] = await Promise.all([api.run(latest.id), loadAllRunEvents(latest.id), api.runArtifacts(latest.id)])
        setRun(detail)
        setEvents(nextEvents)
        setArtifacts(nextArtifacts.items)
        updateConversationRun(detail)
        if (RUNNING_STATUSES.includes(detail.status)) {
          setBusy(true)
          openStream(detail.id, nextEvents.at(-1)?.sequence ?? 0, true)
        } else {
          setConnection('ended')
        }
      }
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setLoadingThread(false)
    }
  }

  const startNewThread = () => {
    streamRef.current?.close()
    setThreadId('')
    setRun(null)
    setConversationRuns([])
    setEvents([])
    setArtifacts([])
    setInput('')
    setSidebarOpen(false)
    setConnection('idle')
    setPendingRoute(null)
    setRoutingNotice(null)
    setDeploymentId(AUTO_DEPLOYMENT)
  }

  const workspaceForRoute = (deploymentIdToUse: string) => {
    const deployment = deployments.find((item) => item.id === deploymentIdToUse)
    if (!deployment?.coding_enabled || !routeRepositoryId) return undefined
    const repository = repositories.find((item) => item.id === routeRepositoryId)
    return {
      repository_id: routeRepositoryId,
      base_ref: routeBaseRef || repository?.default_branch || 'main',
      source_mode: repository?.provider === 'local_snapshot' ? 'working_tree_snapshot' as const : 'committed_ref' as const,
    }
  }

  const commitRouted = async (
    decision: IntentRoutingDecision,
    message: string,
    confirmed = false,
    overrideDeploymentId?: string,
  ) => {
    const targetId = overrideDeploymentId || decision.selected_deployment_id || ''
    const result = await api.createRoutedRun({
      decision_id: decision.id,
      input: message,
      title: message.slice(0, 80),
      confirmed,
      override_deployment_id: overrideDeploymentId,
      workspace: workspaceForRoute(targetId),
    })
    setPendingRoute(null)
    setRoutingNotice(result.decision)
    setThreadId(result.thread.id)
    setDeploymentId(result.thread.agent_deployment_id)
    setRun(result.run)
    updateConversationRun(result.run)
    setInput('')
    openStream(result.run.id)
    await loadThreads()
  }

  const execute = async () => {
    const message = input.trim()
    if (!message || !deploymentId || busy || run?.status === 'WAITING_FOR_APPROVAL') return
    const currentRun = run
    setBusy(true)
    setError('')
    setEvents([])
    setArtifacts([])
    setInspectorTab('events')
    setInspectorOpen(true)
    try {
      let activeThreadId = threadId
      if (!activeThreadId) {
        if (deploymentId === AUTO_DEPLOYMENT) {
          const decision = await api.resolveIntentRoute({ input: message })
          setRouteOverrideId(decision.selected_deployment_id ?? '')
          if (decision.status === 'NEEDS_WORKSPACE' || decision.status === 'NEEDS_CONFIRMATION') {
            setPendingRoute({ decision, message })
            setBusy(false)
            return
          }
          await commitRouted(decision, message)
          return
        }
        const thread = await api.createThread(deploymentId, message.slice(0, 80))
        activeThreadId = thread.id
        setThreadId(thread.id)
      }
      const nextRun = currentRun?.status === 'WAITING_FOR_INPUT'
        ? await api.provideRunInput(currentRun.id, message)
        : await api.createRun(activeThreadId, message)
      setRun(nextRun)
      updateConversationRun(nextRun)
      setInput('')
      openStream(nextRun.id)
      await loadThreads()
    } catch (nextError) {
      setError((nextError as Error).message)
      setBusy(false)
      setConnection('disconnected')
    }
  }

  const cancel = async () => {
    if (!run) return
    try {
      const cancelled = await api.cancelRun(run.id)
      setRun(cancelled)
      updateConversationRun(cancelled)
      streamRef.current?.close()
      setBusy(false)
      setConnection('ended')
    } catch (nextError) {
      setError((nextError as Error).message)
    }
  }

  const activeThread = threads.find((thread) => thread.id === threadId)
  const activeDeployment = deployments.find((deployment) => deployment.id === deploymentId)
  const filteredThreads = useMemo(() => {
    const needle = threadQuery.trim().toLowerCase()
    return needle ? threads.filter((thread) => `${thread.title} ${thread.agent_name ?? ''}`.toLowerCase().includes(needle)) : threads
  }, [threads, threadQuery])
  const canSend = !!input.trim() && !!deploymentId && (deploymentId !== AUTO_DEPLOYMENT || deployments.length > 0) && !busy && run?.status !== 'WAITING_FOR_APPROVAL'
  const pendingTarget = pendingRoute?.decision.candidate_deployments.find((item) => item.id === routeOverrideId)
  const pickerDeployments = deployments.filter((item) => !item.coding_enabled || (!!threadId && item.id === deploymentId))

  return <div className="playground-page">
    {error && <div className="playground-error"><ErrorBanner message={error} /></div>}
    <div className="conversation-workspace">
      <aside id="conversation-history" className={`conversation-sidebar ${sidebarOpen ? 'open' : ''}`} aria-label="Conversation history">
        <div className="conversation-sidebar-head">
          <button className="new-conversation-button" onClick={startNewThread}><Plus size={17} /> New conversation</button>
          <button className="icon-button conversation-sidebar-close" aria-label="Close conversation history" onClick={() => setSidebarOpen(false)}><X size={18} /></button>
        </div>
        <label className="conversation-search"><Search size={15} /><input aria-label="Search conversations" value={threadQuery} onChange={(event) => setThreadQuery(event.target.value)} placeholder="Search conversations" /></label>
        <div className="conversation-history-label"><History size={14} /> Recent</div>
        <div className="conversation-list">
          {filteredThreads.map((thread) => <button className={`conversation-list-item ${thread.id === threadId ? 'selected' : ''}`} aria-current={thread.id === threadId ? 'true' : undefined} onClick={() => void chooseThread(thread.id)} key={thread.id}>
            <MessageSquareText size={16} />
            <span><strong>{thread.title}</strong><small>{thread.agent_name ?? thread.deployment_name ?? 'Agent'} · {formatRelative(thread.updated_at)}</small></span>
            {thread.last_run && <i className={`thread-status status-${thread.last_run.status.toLowerCase().replaceAll('_', '-')}`} title={thread.last_run.status} />}
          </button>)}
          {!filteredThreads.length && <div className="conversation-list-empty">{threads.length ? 'No matching conversations.' : 'Your conversations will appear here.'}</div>}
        </div>
        <div className="conversation-sidebar-foot"><span>{threads.length} conversations</span><Link to="/runs">View all runs</Link></div>
      </aside>

      {sidebarOpen && <button className="conversation-sidebar-scrim" aria-label="Close conversation history" onClick={() => setSidebarOpen(false)} />}

      <section className="chat-stage">
        <header className="conversation-topbar">
          <div className="conversation-title-area">
            <button className="icon-button conversation-menu" aria-label="Open conversation history" aria-controls="conversation-history" aria-expanded={sidebarOpen} onClick={() => setSidebarOpen(true)}><Menu size={19} /></button>
            <div><strong>{activeThread?.title ?? 'New conversation'}</strong><span>{threadId ? shortId(threadId) : 'Start a new thread'}</span></div>
          </div>
          <div className="conversation-topbar-actions">
            <label className="agent-picker"><Sparkles size={16} /><select aria-label="Agent deployment" value={deploymentId} onChange={(event) => setDeploymentId(event.target.value)} disabled={busy || !!threadId}><option value={AUTO_DEPLOYMENT}>Auto · choose from intent</option>{pickerDeployments.map((deployment) => <option value={deployment.id} key={deployment.id}>{deployment.agent_name} · {deployment.environment}</option>)}</select></label>
            <button className={`connection-button state-${connection}`} disabled={connection !== 'disconnected' || !run} onClick={() => run && openStream(run.id, events.at(-1)?.sequence ?? 0, true)} title={connection === 'disconnected' ? 'Reconnect live events' : `Event stream ${connection}`}><i />{connection === 'disconnected' ? <RefreshCw size={13} /> : null}<span>{connection}</span></button>
            <button className={`icon-button inspector-toggle ${inspectorOpen ? 'active' : ''}`} aria-label={`${inspectorOpen ? 'Close' : 'Open'} live activity`} aria-controls="run-inspector" aria-expanded={inspectorOpen} onClick={() => { setInspectorTab('events'); setInspectorOpen((value) => !value) }}><Activity size={18} />{events.length > 0 && <span className="activity-count-badge">{events.length > 99 ? '99+' : events.length}</span>}</button>
          </div>
        </header>

        <div className="chat-scroll" aria-live="polite" aria-busy={busy || loadingThread} ref={chatScrollRef}>
          {loadingThread ? <div className="conversation-loading"><LoaderCircle className="spin" size={24} /> Loading conversation…</div> : conversationRuns.length ? <div className="conversation-transcript">
            {conversationRuns.map((conversationRun) => <ConversationTurn
              key={conversationRun.id}
              run={conversationRun}
              active={conversationRun.id === run?.id}
              agentName={activeDeployment?.agent_name ?? 'DeepAgent'}
              events={conversationRun.id === run?.id ? events : []}
              artifacts={conversationRun.id === run?.id ? artifacts : []}
            />)}
          </div> : <ConversationWelcome userName={context?.user.name} hasDeployment={!!deployments.length} onPrompt={setInput} />}
        </div>

        <div className="playground-composer-zone">
          {run?.status === 'WAITING_FOR_INPUT' && <div className="composer-context"><MessageSquareText size={15} /><span>A reviewer requested changes. Your next message resumes this run as a new attempt.</span></div>}
          {run?.status === 'WAITING_FOR_APPROVAL' && <div className="composer-context approval"><PauseCircle size={15} /><span>This run is waiting for approval before it can continue.</span><Link to="/approvals">Review request</Link></div>}
          {routingNotice && <div className={`composer-context routing ${routingNotice.status === 'FALLBACK' ? 'approval' : ''}`}><Sparkles size={15} /><span><strong>{routingNotice.selected_deployment?.agent_name}</strong> selected for {routingNotice.classification.primary_intent.replaceAll('_', ' ')} · {Math.round(routingNotice.classification.confidence * 100)}% confidence</span></div>}
          <div className={`gemini-composer ${busy ? 'busy' : ''}`}>
            <textarea
              rows={2}
              value={input}
              disabled={busy || run?.status === 'WAITING_FOR_APPROVAL'}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault()
                  if (canSend) void execute()
                }
              }}
              placeholder={run?.status === 'WAITING_FOR_INPUT' ? 'Describe the requested changes…' : deploymentId === AUTO_DEPLOYMENT ? 'Describe your task — Auto will choose an agent…' : `Message ${activeDeployment?.agent_name ?? 'your agent'}…`}
              aria-label="Message your agent"
              aria-describedby="playground-disclaimer"
            />
            <div className="gemini-composer-footer">
              <div><Sparkles size={14} /><span>{deploymentId === AUTO_DEPLOYMENT ? 'Auto intent routing · first message only' : activeDeployment ? `${activeDeployment.agent_name} · ${activeDeployment.environment}` : 'No active deployment'}</span></div>
              {busy ? <button className="composer-send stop" aria-label="Stop run" onClick={() => void cancel()}><Square size={14} fill="currentColor" /></button> : <button className="composer-send" aria-label="Send message" disabled={!canSend} onClick={() => void execute()}><ArrowUp size={18} /></button>}
            </div>
          </div>
          <small id="playground-disclaimer" className="composer-disclaimer">Agents can make mistakes. Review approval requests, artifacts, and the execution trace before using results.</small>
        </div>
      </section>

      {inspectorOpen && <RunInspector run={run} events={events} artifacts={artifacts} tab={inspectorTab} connection={connection} busy={busy} onTab={setInspectorTab} onClose={() => setInspectorOpen(false)} />}
    </div>
    {pendingRoute && <div className="modal-backdrop" onMouseDown={() => setPendingRoute(null)}><div className="modal routing-modal" role="dialog" aria-modal="true" aria-label="Confirm intent routing" onMouseDown={(event) => event.stopPropagation()}>
      <div className="modal-heading"><div><span className="page-eyebrow">INTENT ROUTING</span><h3>{pendingRoute.decision.status === 'NEEDS_WORKSPACE' ? 'Choose a repository' : 'Confirm the recommended agent'}</h3><p>{pendingRoute.decision.classification.summary}</p></div><button aria-label="Close" className="icon-button" onClick={() => setPendingRoute(null)}><X size={18} /></button></div>
      <div className="form-stack routing-confirmation">
        <div className="routing-intent-summary"><Sparkles size={19} /><div><strong>{pendingRoute.decision.classification.primary_intent.replaceAll('_', ' ')}</strong><span>{pendingRoute.decision.classification.subtype.replaceAll('_', ' ')} · {Math.round(pendingRoute.decision.classification.confidence * 100)}% confidence</span></div><StatusPill status={pendingRoute.decision.classification.risk_hint} /></div>
        <label>Agent deployment<select value={routeOverrideId} onChange={(event) => setRouteOverrideId(event.target.value)}>{pendingRoute.decision.candidate_deployments.map((item) => <option value={item.id} key={item.id}>{item.agent_name} · {item.coding_enabled ? 'Coding' : item.knowledge_enabled ? 'Knowledge' : 'General'}</option>)}</select></label>
        {pendingTarget?.coding_enabled && <><label>Repository<select value={routeRepositoryId} onChange={(event) => { const id = event.target.value; setRouteRepositoryId(id); setRouteBaseRef(repositories.find((item) => item.id === id)?.default_branch ?? 'main') }}><option value="">Select repository…</option>{repositories.map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label>Base ref<input value={routeBaseRef} onChange={(event) => setRouteBaseRef(event.target.value)} /></label>{!repositories.length && <div className="coding-empty-note"><FolderOpen size={15} /> Register a working directory in <Link to="/coding">Coding Workbench</Link> first.</div>}</>}
        <div className="routing-request-preview"><span>ORIGINAL REQUEST</span><p>{pendingRoute.message}</p></div>
      </div>
      <div className="modal-actions"><button className="button secondary" onClick={() => { if (routeOverrideId) setDeploymentId(routeOverrideId); setPendingRoute(null) }}>Choose manually</button><button className="button primary" disabled={pendingTarget?.coding_enabled && !routeRepositoryId} onClick={() => { setBusy(true); setError(''); void commitRouted(pendingRoute.decision, pendingRoute.message, true, routeOverrideId !== pendingRoute.decision.selected_deployment_id ? routeOverrideId : undefined).catch((nextError) => { setError((nextError as Error).message); setBusy(false) }) }}>Continue with {pendingTarget?.agent_name ?? 'selected agent'}</button></div>
    </div></div>}
  </div>
}

function ConversationWelcome({ userName, hasDeployment, onPrompt }: { userName?: string; hasDeployment: boolean; onPrompt: (prompt: string) => void }) {
  const suggestions = [
    ['Review a release', 'Identify risks and prepare an auditable recommendation.'],
    ['Research a decision', 'Gather evidence and summarize an implementation strategy.'],
    ['Plan the next step', 'Break a complex project into an executable plan.'],
  ]
  return <div className="conversation-welcome">
    <div className="welcome-spark"><Sparkles size={28} /></div>
    <p>{userName ? `Hi, ${userName.split(' ')[0]}` : 'Hi there'}</p>
    <h2>What can your agent help with?</h2>
    <span>Start a governed conversation. Every run stays attached to this thread with its trace, state, approvals, and artifacts.</span>
    {hasDeployment ? <div className="conversation-suggestions">{suggestions.map(([title, prompt]) => <button onClick={() => onPrompt(prompt)} key={title}><strong>{title}</strong><span>{prompt}</span></button>)}</div> : <div className="no-deployment-callout"><Bot size={20} /><div><strong>No active agent deployment</strong><span>Publish and deploy an agent before starting a conversation.</span></div><Link to="/agents">Open Agents</Link></div>}
  </div>
}

function ConversationTurn({ run, active, agentName, events, artifacts }: { run: Run; active: boolean; agentName: string; events: RuntimeEvent[]; artifacts: RunArtifact[] }) {
  const completedEvent = [...events].reverse().find((event) => event.type === 'model.completed')
  const deltas = events.filter((event) => event.type === 'model.delta').map((event) => String(event.payload.delta ?? '')).join('')
  const reasoningCompletedEvent = [...events].reverse().find((event) => event.type === 'model.reasoning.completed')
  const reasoningDeltas = events.filter((event) => event.type === 'model.reasoning.delta').map((event) => String(event.payload.delta ?? '')).join('')
  const reasoning = String(reasoningCompletedEvent?.payload.reasoning ?? reasoningDeltas)
  const reasoningKind = String(reasoningCompletedEvent?.payload.reasoning_kind ?? [...events].reverse().find((event) => event.type === 'model.reasoning.delta')?.payload.reasoning_kind ?? 'reasoning')
  const response = run.output ?? String(completedEvent?.payload.output ?? deltas)
  const latestEvent = events.at(-1)
  const running = RUNNING_STATUSES.includes(run.status)
  const reasoningActive = running && Boolean(reasoning) && !reasoningCompletedEvent
  const checkpoint = run.checkpoint && typeof run.checkpoint === 'object' && !Array.isArray(run.checkpoint)
    ? run.checkpoint as Record<string, unknown>
    : {}
  const followUpInputs = Array.isArray(checkpoint.responses)
    ? checkpoint.responses.flatMap((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return []
      const responseItem = item as Record<string, unknown>
      return typeof responseItem.input === 'string' && responseItem.input.trim()
        ? [{ input: responseItem.input, receivedAt: typeof responseItem.received_at === 'string' ? responseItem.received_at : run.updated_at }]
        : []
    })
    : []

  return <article className="conversation-turn">
    <div className="user-turn">
      <div className="turn-avatar user">Y</div>
      <div><div className="turn-heading"><strong>You</strong><time title={new Date(run.created_at).toLocaleString()}>{formatRelative(run.created_at)}</time></div><p>{run.input}</p></div>
    </div>
    {followUpInputs.map((item, index) => <div className="user-turn user-turn-follow-up" key={`${run.id}-follow-up-${index}`}>
      <div className="turn-avatar user">Y</div>
      <div><div className="turn-heading"><strong>You</strong><span>Follow-up</span><time title={new Date(item.receivedAt).toLocaleString()}>{formatRelative(item.receivedAt)}</time></div><p>{item.input}</p></div>
    </div>)}
    <div className="assistant-turn">
      <div className="turn-avatar agent"><Sparkles size={17} /></div>
      <div className="assistant-turn-body">
        <div className="turn-heading"><strong>{agentName}</strong><StatusPill status={run.status} /></div>
        {reasoning && <ThinkingPanel content={reasoning} kind={reasoningKind} active={reasoningActive} />}
        {response ? <MarkdownContent className="assistant-copy">{response}</MarkdownContent> : running && !reasoningActive ? <div className="assistant-progress"><LoaderCircle className="spin" size={17} /><div><strong>{latestEvent ? eventLabel(latestEvent.type) : 'Preparing your run'}</strong><span>Live events are available in the run inspector.</span></div></div> : !running && !['WAITING_FOR_APPROVAL', 'WAITING_FOR_INPUT'].includes(run.status) ? <div className="assistant-empty-result">This run ended without a text response.</div> : null}
        {run.status === 'WAITING_FOR_APPROVAL' && <div className="conversation-action-card approval"><PauseCircle size={18} /><div><strong>Approval required</strong><span>This run is checkpointed and waiting for a reviewer.</span></div><Link to="/approvals">Review</Link></div>}
        {run.status === 'WAITING_FOR_INPUT' && <div className="conversation-action-card"><MessageSquareText size={18} /><div><strong>Changes requested</strong><span>Use the composer below to provide revised instructions.</span></div></div>}
        {artifacts.length > 0 && <div className="conversation-artifacts">{artifacts.map((artifact) => <a href={artifact.uri} target="_blank" rel="noreferrer" key={artifact.id}><FileText size={16} /><span><strong>{artifact.name}</strong><small>{artifact.media_type} · {artifact.size_bytes} bytes</small></span></a>)}</div>}
        <div className="turn-footer"><time>{new Date(run.updated_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time><Link to={`/runs/${run.id}`}>View trace <GitBranch size={13} /></Link>{active && running && <span className="live-run-indicator"><i /> Live</span>}</div>
      </div>
    </div>
  </article>
}

function ThinkingPanel({ content, kind, active }: { content: string; kind: string; active: boolean }) {
  const [open, setOpen] = useState(active)

  useEffect(() => {
    if (active) setOpen(true)
  }, [active])

  const label = kind === 'summary' ? 'Reasoning summary' : kind === 'thinking' ? 'Thinking process' : 'Reasoning'
  return <section className={`thinking-panel ${active ? 'active' : ''}`} aria-live={active ? 'polite' : 'off'}>
    <button type="button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
      <span className="thinking-panel-title"><span className="thinking-icon"><Brain size={14} /></span><strong>{active ? 'Thinking…' : label}</strong>{active && <i />}</span>
      <span className="thinking-panel-meta">{active ? 'Streaming live' : `${content.length.toLocaleString()} characters`}<ChevronDown size={14} /></span>
    </button>
    {open && <div className="thinking-panel-content"><MarkdownContent>{content}</MarkdownContent>{active && <span className="thinking-caret" />}</div>}
  </section>
}

function RunInspector({ run, events, artifacts, tab, connection, busy, onTab, onClose }: {
  run: Run | null
  events: RuntimeEvent[]
  artifacts: RunArtifact[]
  tab: InspectorTab
  connection: ConnectionState
  busy: boolean
  onTab: (tab: InspectorTab) => void
  onClose: () => void
}) {
  const [filter, setFilter] = useState<'all' | EventCategory>('all')
  const [followLive, setFollowLive] = useState(true)
  const contentRef = useRef<HTMLDivElement | null>(null)
  const todoEvent = [...events].reverse().find((event) => event.type === 'todo.updated')
  const subagentEvents = events.filter((event) => event.type.startsWith('subagent.'))
  const graphEvents = events.filter((event) => event.type.startsWith('graph.'))
  const filteredEvents = filter === 'all' ? events : events.filter((event) => eventCategoryKey(event) === filter)
  const eventCounts = useMemo(() => EVENT_FILTERS.reduce<Record<string, number>>((counts, item) => {
    counts[item.key] = item.key === 'all' ? events.length : events.filter((event) => eventCategoryKey(event) === item.key).length
    return counts
  }, {}), [events])

  useEffect(() => {
    if (tab === 'events' && followLive) {
      const content = contentRef.current
      content?.scrollTo({ top: content.scrollHeight, behavior: events.length > 1 ? 'smooth' : 'auto' })
    }
  }, [events.length, filter, followLive, tab])

  return <aside id="run-inspector" className="playground-inspector" aria-label="Live execution activity">
    <div className="playground-inspector-head">
      <div><span>LIVE EXECUTION</span><strong><i className={`live-connection-dot state-${connection}`} />{busy ? 'Following backend events' : run ? `${events.length} events captured` : 'Waiting for a run'}</strong></div>
      <div>{run && <Link className="button ghost" to={`/runs/${run.id}`}>Full trace</Link>}<button className="icon-button" aria-label="Close live activity" onClick={onClose}><X size={18} /></button></div>
    </div>
    <div className="inspector-tabs" role="tablist">{(['events', 'plan', 'state', 'artifacts', 'usage'] as InspectorTab[]).map((item) => <button role="tab" aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => onTab(item)} key={item}>{item === 'plan' ? <CheckCircle2 size={14} /> : item === 'events' ? <Activity size={14} /> : item === 'state' ? <Boxes size={14} /> : item === 'artifacts' ? <FileText size={14} /> : <Clock3 size={14} />}{item}{item === 'events' && events.length > 0 && <i>{events.length > 99 ? '99+' : events.length}</i>}{item === 'artifacts' && artifacts.length > 0 && <i>{artifacts.length}</i>}</button>)}</div>
    {tab === 'events' && <div className="activity-filter-shell">
      <div className="activity-filter-bar" aria-label="Filter runtime events">{EVENT_FILTERS.map((item) => <button className={filter === item.key ? 'active' : ''} aria-pressed={filter === item.key} onClick={() => setFilter(item.key)} key={item.key}><span>{item.label}</span><i>{eventCounts[item.key] ?? 0}</i></button>)}</div>
      <div className="activity-live-controls"><span><i className={`live-connection-dot state-${connection}`} />{connection === 'connected' ? 'Live stream connected' : connection === 'ended' ? 'Run event stream ended' : `Stream ${connection}`}</span><button className={followLive ? 'active' : ''} aria-pressed={followLive} onClick={() => setFollowLive((value) => !value)}>{followLive ? 'Following' : 'Follow live'}</button></div>
    </div>}
    <div className="playground-inspector-content" ref={contentRef}>
      {run && <div className="run-summary-card"><div><span>RUN</span><code>{shortId(run.id)}</code></div><StatusPill status={run.status} /><div className="run-summary-meta"><span><GitBranch size={13} />{shortId(run.resolved_plan_id, 6)}</span><span><Clock3 size={13} />Attempt {run.attempts?.length ?? 1}</span></div></div>}
      {!run ? <InspectorEmpty icon={Activity} title="Waiting for live activity" text="Start or open a conversation. Every backend event will appear here in sequence." /> : tab === 'events' ? (filteredEvents.length ? <EventTimeline events={filteredEvents} /> : <InspectorEmpty icon={Terminal} title="No matching events" text={events.length ? 'Choose another event category.' : 'Backend events appear here as soon as the run starts.'} />) : tab === 'plan' ? <>{graphEvents.length > 0 && <div className="graph-summary-card"><div className="inspector-section-title"><GitBranch size={15} /><strong>Execution graph</strong><span>{graphEvents.length} events</span></div><div className="graph-summary-flow"><span>Main graph</span><i /><span>{new Set(graphEvents.flatMap((event) => event.execution_path.slice(1))).size} nodes</span><i /><span>{subagentEvents.length ? `${new Set(subagentEvents.map((event) => event.execution_path.join('/'))).size} subgraph` : 'No subgraph'}</span></div></div>}{todoEvent && <div className="todo-card"><div className="inspector-section-title"><CheckCircle2 size={15} /><strong>Agent plan</strong><span>{todoEvent.payload.items.filter((item: any) => item.status === 'completed').length}/{todoEvent.payload.items.length}</span></div>{todoEvent.payload.items.map((item: any) => <div className={`todo-item ${item.status}`} key={item.id}><span>{item.status === 'completed' ? <CheckCircle2 size={14} /> : item.status === 'in_progress' ? <LoaderCircle className="spin" size={14} /> : <Clock3 size={14} />}</span><p>{item.title}</p></div>)}</div>}{subagentEvents.length > 0 && <div className="subagent-card"><div className="inspector-section-title"><Bot size={15} /><strong>SubAgent tree</strong><span>{new Set(subagentEvents.map((event) => event.execution_path.join('/'))).size} child</span></div><div className="subagent-node"><div className="tree-line" /><div className="agent-logo tiny"><Bot size={13} /></div><div><strong>{String(subagentEvents[0]?.payload.name ?? subagentEvents[0]?.payload.agent_name ?? 'researcher')}</strong><span>{subagentEvents.at(-1)?.type === 'subagent.completed' ? 'Completed' : 'Working'}</span></div></div></div>}{!todoEvent && !subagentEvents.length && !graphEvents.length && <InspectorEmpty icon={CheckCircle2} title="No plan yet" text="Graph, plan, and SubAgent activity appear here during a run." />}</> : tab === 'state' ? <pre className="state-view">{JSON.stringify({ status: run.status, checkpoint: run.checkpoint, metadata: run.metadata, attempts: run.attempts }, null, 2)}</pre> : tab === 'artifacts' ? (artifacts.length ? artifacts.map((artifact) => <a className="artifact-card" href={artifact.uri} target="_blank" rel="noreferrer" key={artifact.id}><div className="artifact-icon"><FileText size={18} /></div><div><strong>{artifact.name}</strong><span>{artifact.media_type} · {artifact.size_bytes} bytes</span><code>{artifact.content_hash.slice(0, 18)}…</code></div></a>) : <InspectorEmpty icon={FileText} title="No artifacts yet" text="Run outputs appear here with their content hash." />) : run.usage ? <div className="usage-detail compact"><div><span>Input tokens</span><strong>{run.usage.input_tokens}</strong></div><div><span>Output tokens</span><strong>{run.usage.output_tokens}</strong></div><div><span>Model calls</span><strong>{run.usage.model_calls}</strong></div><div><span>Tool calls</span><strong>{run.usage.tool_calls}</strong></div><div><span>SubAgent calls</span><strong>{run.usage.subagent_calls}</strong></div><div><span>Cost</span><strong>${run.usage.cost.toFixed(4)}</strong></div></div> : <InspectorEmpty icon={Clock3} title="No usage yet" text="Usage appears after the runtime reports it." />}
    </div>
  </aside>
}

function InspectorEmpty({ icon: Icon, title, text }: { icon: typeof Terminal; title: string; text: string }) {
  return <div className="inspector-empty"><Icon size={24} /><h4>{title}</h4><p>{text}</p></div>
}

function eventLabel(type: string) {
  return type.replaceAll('.', ' · ').replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}
