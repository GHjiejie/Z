import {
  ArrowUp,
  CheckCircle2,
  ChevronRight,
  Code2,
  FileCode2,
  FileDiff,
  Files,
  Folder,
  FolderOpen,
  GitBranch,
  LoaderCircle,
  Play,
  RefreshCw,
  ShieldAlert,
  Square,
  TerminalSquare,
  TestTube2,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ErrorBanner, StatusPill, shortId } from '../components/UI'
import { api, streamUrl } from '../lib/api'
import type {
  ChangeSet,
  CodingWorkspace,
  Deployment,
  Interrupt,
  LocalRepositoryFolderListing,
  Repository,
  Run,
  RunArtifact,
  RuntimeEvent,
  ThreadSummary,
  VerificationReport,
  WorkspaceTreeItem,
} from '../types'

const ACTIVE = ['CREATED', 'QUEUED', 'PREPARING', 'RUNNING', 'RESUMING']
type CenterTab = 'code' | 'diff' | 'verification' | 'commands'

export function CodingWorkbenchPage() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [repositories, setRepositories] = useState<Repository[]>([])
  const [threads, setThreads] = useState<ThreadSummary[]>([])
  const [deploymentId, setDeploymentId] = useState('')
  const [repositoryId, setRepositoryId] = useState('')
  const [baseRef, setBaseRef] = useState('main')
  const [task, setTask] = useState('')
  const [title, setTitle] = useState('New coding task')
  const [run, setRun] = useState<Run | null>(null)
  const [workspace, setWorkspace] = useState<CodingWorkspace | null>(null)
  const [tree, setTree] = useState<WorkspaceTreeItem[]>([])
  const [selectedPath, setSelectedPath] = useState('')
  const [fileContent, setFileContent] = useState('')
  const [diff, setDiff] = useState<ChangeSet | null>(null)
  const [verification, setVerification] = useState<VerificationReport | null>(null)
  const [artifacts, setArtifacts] = useState<RunArtifact[]>([])
  const [events, setEvents] = useState<RuntimeEvent[]>([])
  const [interrupt, setInterrupt] = useState<Interrupt | null>(null)
  const [tab, setTab] = useState<CenterTab>('diff')
  const [splitDiff, setSplitDiff] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [folderPickerOpen, setFolderPickerOpen] = useState(false)
  const [folderListing, setFolderListing] = useState<LocalRepositoryFolderListing | null>(null)
  const [folderPath, setFolderPath] = useState('')
  const [folderRepositoryName, setFolderRepositoryName] = useState('')
  const [folderBranch, setFolderBranch] = useState('main')
  const [folderBusy, setFolderBusy] = useState(false)
  const [folderError, setFolderError] = useState('')
  const streamRef = useRef<EventSource | null>(null)

  const loadCatalog = async () => {
    const [deploymentResult, repositoryResult, threadResult] = await Promise.all([
      api.deployments(), api.repositories(), api.threads(),
    ])
    const codingDeployments = deploymentResult.items.filter((item) => item.coding_enabled)
    setDeployments(codingDeployments)
    setRepositories(repositoryResult.items)
    setThreads(threadResult.items.filter((item) => !!item.repository_id))
    if (!deploymentId && codingDeployments[0]) setDeploymentId(codingDeployments[0].id)
    if (!repositoryId && repositoryResult.items[0]) {
      setRepositoryId(repositoryResult.items[0].id)
      setBaseRef(repositoryResult.items[0].default_branch)
    }
  }

  useEffect(() => {
    loadCatalog().catch((nextError) => setError((nextError as Error).message))
    return () => streamRef.current?.close()
  }, [])

  const browseFolders = async (path?: string) => {
    setFolderBusy(true); setFolderError(''); setFolderListing(null)
    try {
      const listing = await api.localRepositoryFolders(path)
      setFolderListing(listing)
      setFolderPath(listing.current_path)
      setFolderRepositoryName(listing.current.name)
      setFolderBranch(listing.current.default_branch || 'working-directory')
    } catch (nextError) {
      setFolderRepositoryName('')
      setFolderBranch('working-directory')
      setFolderError((nextError as Error).message)
    } finally { setFolderBusy(false) }
  }

  const openFolderPicker = () => {
    setFolderError('')
    setFolderPickerOpen(true)
    void browseFolders()
  }

  const registerSelectedFolder = async () => {
    const selected = folderListing?.current
    if (!selected || !folderRepositoryName.trim()) return
    const existing = repositories.find((item) => item.canonical_uri === selected.path)
    if (existing) {
      setRepositoryId(existing.id); setBaseRef(existing.default_branch); setFolderPickerOpen(false)
      return
    }
    setFolderBusy(true); setFolderError('')
    try {
      const repository = await api.createRepository({
        name: folderRepositoryName.trim(),
        provider: 'local_snapshot',
        canonical_uri: selected.path,
        default_branch: folderBranch || 'working-directory',
      })
      await loadCatalog()
      setRepositoryId(repository.id); setBaseRef(repository.default_branch); setFolderPickerOpen(false)
    } catch (nextError) { setFolderError((nextError as Error).message) } finally { setFolderBusy(false) }
  }

  const refreshRun = async (runId: string) => {
    const detail = await api.run(runId)
    setRun(detail)
    const nextWorkspace = await api.threadWorkspace(detail.thread_id)
    setWorkspace(nextWorkspace)
    const treeRequest = nextWorkspace.sandbox?.status === 'ACTIVE'
      ? api.workspaceTree(runId).catch(() => null)
      : Promise.resolve(null)
    const [eventResult, artifactResult, treeResult, diffResult, verificationResult, interrupts] = await Promise.all([
      api.runEvents(runId),
      api.runArtifacts(runId),
      treeRequest,
      api.runDiff(runId).catch(() => null),
      api.runVerification(runId).catch(() => null),
      api.interrupts('PENDING'),
    ])
    setEvents(eventResult.items)
    setArtifacts(artifactResult.items)
    setTree(treeResult?.items ?? [])
    if (diffResult && 'id' in diffResult) setDiff(diffResult)
    if (verificationResult) setVerification(verificationResult)
    setInterrupt(interrupts.items.find((item) => item.run_id === runId) ?? null)
    if (!ACTIVE.includes(detail.status)) setBusy(false)
    return detail
  }

  const openStream = (runId: string, after = 0) => {
    streamRef.current?.close()
    const source = new EventSource(streamUrl(runId, after))
    streamRef.current = source
    source.addEventListener('runtime.event', (message) => {
      const event = JSON.parse((message as MessageEvent).data) as RuntimeEvent
      setEvents((previous) => previous.some((item) => item.event_id === event.event_id) ? previous : [...previous, event])
      if (['file.changed', 'verification.completed', 'changeset.created', 'tool.approval_required', 'run.completed', 'run.failed'].includes(event.type)) {
        void refreshRun(runId)
      }
    })
    source.addEventListener('stream.idle', () => { source.close(); void refreshRun(runId) })
    source.onerror = () => { source.close(); void refreshRun(runId) }
  }

  const start = async () => {
    if (!deploymentId || !repositoryId || !task.trim()) return
    setBusy(true); setError(''); setTree([]); setDiff(null); setVerification(null); setEvents([]); setArtifacts([])
    try {
      const repository = repositories.find((item) => item.id === repositoryId)
      const sourceMode = repository?.provider === 'local_snapshot' ? 'working_tree_snapshot' : 'committed_ref'
      const thread = await api.createCodingThread(deploymentId, title || task.slice(0, 80), repositoryId, baseRef, sourceMode)
      const nextRun = await api.createRun(thread.id, task)
      setRun(nextRun)
      setWorkspace(await api.threadWorkspace(thread.id))
      openStream(nextRun.id)
      await loadCatalog()
    } catch (nextError) { setError((nextError as Error).message); setBusy(false) }
  }

  const openThread = async (thread: ThreadSummary) => {
    setError(''); setBusy(false); streamRef.current?.close()
    try {
      const [detail, nextWorkspace] = await Promise.all([api.thread(thread.id), api.threadWorkspace(thread.id)])
      setWorkspace(nextWorkspace)
      setRepositoryId(thread.repository_id ?? '')
      const latest = detail.runs?.[0]
      if (!latest) return
      const nextRun = await refreshRun(latest.id)
      if (ACTIVE.includes(nextRun.status)) { setBusy(true); openStream(nextRun.id, events.at(-1)?.sequence ?? 0) }
    } catch (nextError) { setError((nextError as Error).message) }
  }

  const openFile = async (path: string) => {
    if (!run) return
    setSelectedPath(path); setTab('code')
    try {
      const file = await api.workspaceFile(run.id, path)
      setFileContent(file.encoding === 'utf-8' ? file.content : '[Binary file — content omitted]')
    } catch (nextError) { setFileContent(''); setError((nextError as Error).message) }
  }

  const decideApproval = async (type: 'approve' | 'reject') => {
    if (!interrupt) return
    try {
      await api.decide(interrupt, type)
      setInterrupt(null); setBusy(true); openStream(interrupt.run_id, events.at(-1)?.sequence ?? 0)
    } catch (nextError) { setError((nextError as Error).message) }
  }

  const decideChangeSet = async (approve: boolean) => {
    if (!run || !diff) return
    try {
      setDiff(await api.decideChangeSet(run.id, diff.id, approve))
      await refreshRun(run.id)
    } catch (nextError) { setError((nextError as Error).message) }
  }

  const commands = useMemo(() => events.filter((event) => ['sandbox.command.completed', 'sandbox.command.failed', 'sandbox.command.denied'].includes(event.type)), [events])
  const patchArtifact = artifacts.find((artifact) => artifact.name === 'changes.patch')
  const changedPaths = new Set(diff?.changed_files.map((item) => `/workspace/repo/${item.path}`) ?? [])

  return <div className="coding-page">
    {error && <ErrorBanner message={error} />}
    <header className="coding-launch panel">
      <div><span className="page-eyebrow">ISOLATED CODING</span><h2>Coding Workbench</h2><p>Agent changes an immutable repository snapshot inside a governed sandbox and delivers a reviewable patch.</p></div>
      <div className="coding-launch-fields">
        <label>Agent deployment<select value={deploymentId} onChange={(event) => setDeploymentId(event.target.value)}><option value="">Select coding agent…</option>{deployments.map((item) => <option key={item.id} value={item.id}>{item.agent_name} · {item.environment}</option>)}</select></label>
        <label>Working directory<div className="repository-select"><select value={repositoryId} onChange={(event) => { const id = event.target.value; setRepositoryId(id); setBaseRef(repositories.find((item) => item.id === id)?.default_branch ?? 'working-directory') }}><option value="">Select working directory…</option>{repositories.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select><button type="button" onClick={openFolderPicker}><FolderOpen size={14} /> Choose folder</button></div></label>
        <label>Base ref<input value={baseRef} onChange={(event) => setBaseRef(event.target.value)} /></label>
      </div>
      <div className="coding-task-row"><input aria-label="Task title" value={title} onChange={(event) => setTitle(event.target.value)} /><textarea aria-label="Coding task" rows={2} value={task} onChange={(event) => setTask(event.target.value)} placeholder="Describe the code change and acceptance criteria…" /><button className="button primary" disabled={busy || !task.trim() || !repositoryId || !deploymentId} onClick={() => void start()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />} Start</button>{run && ACTIVE.includes(run.status) && <button className="button danger" onClick={() => void api.cancelRun(run.id).then(setRun)}><Square size={13} /> Cancel</button>}</div>
      {!deployments.length && <div className="coding-empty-note"><ShieldAlert size={15} /> No Coding Agent deployment. Create one from the <Link to="/agents">Coding Agent starter</Link>.</div>}
      {!repositories.length && <div className="coding-empty-note"><FolderOpen size={15} /> Choose a local working directory before starting.</div>}
    </header>

    <div className="coding-workbench">
      <aside className="coding-explorer panel">
        <div className="coding-pane-head"><div><Files size={15} /><strong>Working directory</strong></div><button className="icon-button" aria-label="Refresh workspace" disabled={!run} onClick={() => run && void refreshRun(run.id)}><RefreshCw size={14} /></button></div>
        <div className="workspace-facts"><span>{workspace?.repository_name ?? repositories.find((item) => item.id === repositoryId)?.name ?? 'No workspace'}</span><code>{workspace?.resolved_commit_sha?.slice(0, 12) ?? baseRef}</code><StatusPill status={workspace?.status ?? 'UNPROVISIONED'} /></div>
        <div className="file-tree">{tree.map((item) => <button key={item.path} className={selectedPath === item.path ? 'selected' : ''} onClick={() => void openFile(item.path)}><FileCode2 size={13} /><span>{item.path.replace('/workspace/repo/', '')}</span>{changedPaths.has(item.path) && <i />}</button>)}{!tree.length && <div className="tree-empty">Files appear after the sandbox is ready.</div>}</div>
        <div className="recent-coding"><strong>Recent coding tasks</strong>{threads.slice(0, 7).map((thread) => <button key={thread.id} onClick={() => void openThread(thread)}><span>{thread.title}</span><ChevronRight size={13} /></button>)}</div>
      </aside>

      <section className="coding-center panel">
        <div className="coding-tabs">{([['code', Code2, 'Code'], ['diff', FileDiff, 'Diff'], ['verification', TestTube2, 'Tests'], ['commands', TerminalSquare, 'Commands']] as const).map(([id, Icon, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}><Icon size={14} />{label}</button>)}<div className="coding-center-actions">{tab === 'diff' && <button onClick={() => setSplitDiff(!splitDiff)}>{splitDiff ? 'Unified' : 'Split'}</button>}{patchArtifact && <a className="button secondary" href={patchArtifact.uri}>Download patch</a>}</div></div>
        {tab === 'code' && <div className="code-view"><div className="code-view-path">{selectedPath || 'Select a file'}</div><pre>{fileContent || 'Choose a text file from the repository tree.'}</pre></div>}
        {tab === 'diff' && <DiffView patch={diff?.patch ?? ''} split={splitDiff} changeSet={diff} />}
        {tab === 'verification' && <div className="verification-view"><div className="verification-summary"><StatusPill status={verification?.status ?? 'PENDING'} /><span>{verification?.summary.passed ?? 0} passed · {verification?.summary.failed ?? 0} failed</span></div>{verification?.checks.map((check) => <article key={check.id}><div>{check.status === 'passed' ? <CheckCircle2 size={15} /> : <ShieldAlert size={15} />}<code>{check.command}</code><StatusPill status={check.status} /></div><pre>{check.output_preview || `exit ${check.exit_code}`}</pre></article>) ?? <div className="center-empty">Verification has not run yet.</div>}</div>}
        {tab === 'commands' && <div className="command-view">{commands.map((event) => <article key={event.event_id}><div><TerminalSquare size={14} /><code>{String(event.payload.command_id ?? '')}</code><StatusPill status={event.type.endsWith('denied') ? 'DENIED' : event.type.endsWith('failed') ? 'FAILED' : 'SUCCEEDED'} /></div><span>exit {String(event.payload.exit_code ?? '—')} · {String(event.payload.duration_ms ?? '—')} ms</span></article>)}{!commands.length && <div className="center-empty">Sandbox command evidence will appear here.</div>}</div>}
      </section>

      <aside className="coding-agent-pane panel">
        <div className="coding-pane-head"><div><Code2 size={15} /><strong>Agent</strong></div>{run && <StatusPill status={run.status} />}</div>
        <div className="agent-run-facts"><span>Run <code>{run ? shortId(run.id) : '—'}</code></span><span>Generation <strong>{workspace?.workspace_generation ?? 0}</strong></span><span>Plan <code>{diff?.plan_hash?.slice(0, 12) ?? '—'}</code></span></div>
        <div className="agent-conversation"><div className="user-task"><strong>Task</strong><p>{run?.input ?? 'Start a coding task to begin.'}</p></div>{run?.output && <div className="agent-result"><strong>Agent report</strong><p>{run.output}</p></div>}<div className="live-agent-events">{events.slice(-18).map((event) => <div key={event.event_id}><i /><span>{event.type.replaceAll('.', ' ')}</span><small>#{event.sequence}</small></div>)}</div></div>
        {interrupt && <div className="approval-card"><ShieldAlert size={18} /><div><strong>Approval required</strong><p>{interrupt.policy_reason}</p><code>{interrupt.actions[0]?.tool_name}</code></div><div><button className="button danger" onClick={() => void decideApproval('reject')}>Reject</button><button className="button approve" onClick={() => void decideApproval('approve')}>Approve</button></div></div>}
        {diff && <div className="changeset-card"><div><FileDiff size={16} /><strong>ChangeSet</strong><StatusPill status={diff.status} /></div><span>{diff.diff_stat.files} files · <b>+{diff.diff_stat.added}</b> · <em>-{diff.diff_stat.deleted}</em></span><code>{diff.content_hash.slice(0, 16)}</code>{!['DELIVERED', 'REJECTED'].includes(diff.status) && <div className="changeset-actions"><button className="button danger" onClick={() => void decideChangeSet(false)}>Reject patch</button><button className="button approve" onClick={() => void decideChangeSet(true)}>Approve patch</button></div>}</div>}
      </aside>
    </div>
    {folderPickerOpen && <div className="modal-backdrop" onMouseDown={() => setFolderPickerOpen(false)}><div className="modal folder-picker-modal" role="dialog" aria-modal="true" aria-label="Choose a local working directory" onMouseDown={(event) => event.stopPropagation()}>
      <div className="modal-heading"><div><span className="page-eyebrow">LOCAL WORKSPACE</span><h3>Choose a folder</h3><p>Git is optional. Browse only within administrator-approved local roots.</p></div><button aria-label="Close" className="icon-button" onClick={() => setFolderPickerOpen(false)}><X size={18} /></button></div>
      <div className="folder-picker-body">
        <div className="folder-path-row"><input aria-label="Local folder path" value={folderPath} onChange={(event) => setFolderPath(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void browseFolders(folderPath) }} /><button className="button secondary" disabled={folderBusy || !folderPath.trim()} onClick={() => void browseFolders(folderPath)}>Go</button></div>
        {folderError && <div className="folder-picker-error" role="alert"><ShieldAlert size={15} /><span>{folderError}</span></div>}
        <div className="folder-roots">{folderListing?.roots.map((root) => <button key={root} onClick={() => void browseFolders(root)}><FolderOpen size={13} />{root}</button>)}</div>
        <div className="folder-browser-list">
          {folderListing?.parent_path && <button onClick={() => void browseFolders(folderListing.parent_path!)}><ArrowUp size={14} /><span>..</span><small>Parent folder</small></button>}
          {folderListing?.items.map((item) => <button key={item.path} onClick={() => { setFolderPath(item.path); void browseFolders(item.path) }}><Folder size={14} /><span>{item.name}</span>{item.is_git_repository && <small><GitBranch size={11} /> Git</small>}</button>)}
          {!folderBusy && !folderListing?.items.length && <div className="folder-browser-empty">No child folders.</div>}
          {folderBusy && <div className="folder-browser-empty"><LoaderCircle className="spin" size={17} /> Loading folders…</div>}
        </div>
        {folderListing?.current && <div className="folder-selection"><div>{folderListing.current.is_git_repository ? <GitBranch size={16} /> : <FolderOpen size={16} />}<span><strong>{folderListing.current.path}</strong><small>{folderListing.current.is_git_repository ? 'Git-backed working directory' : 'Local working directory'} · ready to register</small></span></div><div className="form-row"><label>Workspace name<input value={folderRepositoryName} onChange={(event) => setFolderRepositoryName(event.target.value)} /></label><label>{folderListing.current.is_git_repository ? 'Default branch' : 'Snapshot label'}<input value={folderBranch} onChange={(event) => setFolderBranch(event.target.value)} /></label></div></div>}
      </div>
      <div className="modal-actions"><button className="button secondary" onClick={() => setFolderPickerOpen(false)}>Cancel</button><button className="button primary" disabled={folderBusy || !folderListing?.current || !folderRepositoryName.trim()} onClick={() => void registerSelectedFolder()}>{folderBusy && <LoaderCircle className="spin" size={14} />} Use working directory</button></div>
    </div></div>}
  </div>
}

function DiffView({ patch, split, changeSet }: { patch: string; split: boolean; changeSet: ChangeSet | null }) {
  if (!patch) return <div className="center-empty">A platform-computed diff appears when the run reaches a safe boundary.</div>
  if (!split) return <div className="diff-view"><div className="diff-summary">{changeSet?.diff_stat.files ?? 0} files <b>+{changeSet?.diff_stat.added ?? 0}</b> <em>-{changeSet?.diff_stat.deleted ?? 0}</em></div><pre>{patch}</pre></div>
  const lines = patch.split('\n')
  return <div className="split-diff"><pre>{lines.filter((line) => !line.startsWith('+') || line.startsWith('+++')).join('\n')}</pre><pre>{lines.filter((line) => !line.startsWith('-') || line.startsWith('---')).join('\n')}</pre></div>
}
