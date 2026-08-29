import {
  AlertTriangle,
  Bot,
  Box,
  Check,
  ChevronRight,
  CircleCheck,
  Code2,
  Copy,
  Database,
  FileCode2,
  GitBranch,
  Layers3,
  LoaderCircle,
  Plus,
  Rocket,
  Save,
  Settings2,
  ShieldCheck,
  Sparkles,
  Wrench,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from '../lib/api'
import type { Agent, AgentDraft, Deployment, KnowledgeBase, ModelDeployment, Revision, Skill } from '../types'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'
import { usePlatform } from '../context/PlatformContext'

const defaultDraft: AgentDraft = {
  harness_type: 'deepagents', harness_profile_revision_id: 'deepagents-0.x-adapter-1.0', model_deployment_id: 'model_qwen_prod_v1',
  system_prompt: 'You are a careful project agent. Plan first, use approved tools, and cite artifacts.',
  capabilities: { tools: ['knowledge_search', 'artifact_write'], mcp_servers: [], skills: ['task-planning', 'release-safety'], memories: [], knowledge_bases: [], subagents: ['researcher'], filesystem: true },
  policies: { permission_policy: 'project-default', approval_mode: 'high_risk', audit_level: 'strict' },
  limits: { max_duration_seconds: 600, max_model_calls: 20, max_tool_calls: 30, max_subagent_depth: 3, max_subagent_concurrency: 4, max_sandbox_cpu_seconds: 120, max_output_bytes: 1000000, max_cost: 5 }, output_schema: null,
}

type BuilderTab = 'basics' | 'model' | 'capabilities' | 'guardrails' | 'review' | 'release'
type ValidationIssue = { level: string; message: string }
type BindingOption = { value: string; label: string; detail?: string; status?: string }
type PublishResult = { revision: { id: string; revision_number: number }; resolved_plan: { id: string; plan_hash: string }; deployment?: Deployment }

const tabs: Array<{ id: BuilderTab; label: string; icon: typeof Bot }> = [
  { id: 'basics', label: 'Basics', icon: Settings2 }, { id: 'model', label: 'Model', icon: Sparkles },
  { id: 'capabilities', label: 'Capabilities', icon: Wrench }, { id: 'guardrails', label: 'Guardrails', icon: ShieldCheck },
  { id: 'review', label: 'Review', icon: CircleCheck }, { id: 'release', label: 'Releases', icon: GitBranch },
]

export function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [models, setModels] = useState<ModelDeployment[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBase[]>([])
  const [selected, setSelected] = useState<Agent | null>(null)
  const [draft, setDraft] = useState<AgentDraft>(structuredClone(defaultDraft))
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tab, setTab] = useState<BuilderTab>('basics')
  const [creating, setCreating] = useState(false)
  const [createName, setCreateName] = useState('')
  const [createDescription, setCreateDescription] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [publishEnvironment, setPublishEnvironment] = useState('development')
  const [publishResult, setPublishResult] = useState<PublishResult | null>(null)
  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [searchParams, setSearchParams] = useSearchParams()
  const { refresh: refreshPlatform } = usePlatform()

  const selectAgent = async (id: string) => {
    try {
      const agent = await api.agent(id)
      setSelected(agent); setDraft(structuredClone(agent.draft)); setName(agent.name); setDescription(agent.description); setNotice(''); setIssues([])
      const next = new URLSearchParams(searchParams); next.set('agent', id); setSearchParams(next, { replace: true })
    } catch (nextError) { setError((nextError as Error).message) }
  }

  const load = async () => {
    try {
      const [agentResult, modelResult, skillResult, knowledgeResult] = await Promise.all([api.agents(), api.models(), api.skills(), api.knowledgeBases()])
      setAgents(agentResult.items); setModels(modelResult.items); setSkills(skillResult.items); setKnowledgeBases(knowledgeResult.items)
      if (!selected && agentResult.items[0]) {
        const requestedId = searchParams.get('agent')
        await selectAgent(agentResult.items.some((agent) => agent.id === requestedId) ? requestedId! : agentResult.items[0].id)
      }
    } catch (nextError) { setError((nextError as Error).message) } finally { setLoaded(true) }
  }
  useEffect(() => { void load() }, [])

  const dirty = useMemo(() => !!selected && (name !== selected.name || description !== selected.description || JSON.stringify(draft) !== JSON.stringify(selected.draft)), [selected, name, description, draft])
  useEffect(() => {
    const guard = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault() }
    window.addEventListener('beforeunload', guard); return () => window.removeEventListener('beforeunload', guard)
  }, [dirty])

  const save = async () => {
    if (!selected) return null
    setBusy('save'); setError(''); setNotice('')
    try {
      const updated = await api.updateAgent(selected.id, { name, description, draft, version: selected.version })
      setSelected(updated); setDraft(structuredClone(updated.draft)); setNotice('Draft saved.'); await load(); return updated
    } catch (nextError) { setError((nextError as Error).message); return null } finally { setBusy('') }
  }

  const validate = async () => {
    if (!selected) return false
    if (dirty && !await save()) return false
    setBusy('validate'); setError(''); setNotice('')
    try {
      const result = await api.validateAgent(selected.id); setIssues(result.issues)
      setNotice(result.valid ? 'Validation passed. The draft is ready for review.' : 'Validation found blocking issues.')
      return result.valid
    } catch (nextError) { setError((nextError as Error).message); return false } finally { setBusy('') }
  }

  const reviewPublish = async () => {
    if (await validate()) { setPublishResult(null); setPublishing(true) }
  }

  const publishRevision = async () => {
    if (!selected) return
    setBusy('publish'); setError('')
    try {
      const result = await api.publishAgent(selected.id); setPublishResult(result); await load(); await selectAgent(selected.id); await refreshPlatform()
    } catch (nextError) { setError((nextError as Error).message); setPublishing(false) } finally { setBusy('') }
  }

  const deployRevision = async () => {
    if (!publishResult) return
    setBusy('deploy')
    try {
      const deployment = await api.deploy(publishResult.revision.id, publishEnvironment)
      setPublishResult({ ...publishResult, deployment }); await load(); await refreshPlatform()
    } catch (nextError) { setError((nextError as Error).message) } finally { setBusy('') }
  }

  const create = async () => {
    if (!createName.trim()) return
    setBusy('create')
    try {
      const agent = await api.createAgent({ name: createName, description: createDescription, draft: structuredClone(defaultDraft) })
      setCreating(false); setCreateName(''); setCreateDescription(''); await load(); await selectAgent(agent.id); await refreshPlatform()
    } catch (nextError) { setError((nextError as Error).message) } finally { setBusy('') }
  }

  const updateCapabilities = (key: keyof AgentDraft['capabilities'], value: AgentDraft['capabilities'][typeof key]) => setDraft({ ...draft, capabilities: { ...draft.capabilities, [key]: value } })
  const discard = () => { if (selected) { setName(selected.name); setDescription(selected.description); setDraft(structuredClone(selected.draft)); setIssues([]) } }
  const changedSections = selected ? getChangedSections(selected.revisions?.[0], { ...draft }, name, description, selected) : []
  const knowledgeOptions = knowledgeBases.filter((base) => base.current_revision_id).map((base) => ({ value: base.current_revision_id!, label: base.name, detail: `Active revision · ${base.ready_document_count ?? 0} documents`, status: base.status }))

  if (!loaded) return <LoadingBlock />

  return <div className="page-stack agents-page">
    <PageHeader eyebrow="AGENT REGISTRY" title="Build and release agents" description="Configure a draft, validate its bindings, review immutable changes, then choose where to deploy." actions={<button className="button primary" onClick={() => setCreating(true)}><Plus size={16} /> New agent</button>} />
    {error && <ErrorBanner message={error} />}
    <div className="builder-layout">
      <aside className="agent-list-panel panel"><div className="list-search"><Bot size={16} /><span>{agents.length} registered agents</span></div><div className="agent-list">
        {agents.map((agent) => <button key={agent.id} className={`agent-list-item ${selected?.id === agent.id ? 'selected' : ''}`} onClick={() => { if (!dirty || window.confirm('Discard unsaved changes?')) void selectAgent(agent.id) }}><div className="agent-logo"><Sparkles size={17} /></div><div className="agent-list-copy"><strong>{agent.name}</strong><span>{agent.description || 'No description'}</span><small>{agent.latest_deployment ? `${agent.latest_deployment.environment} deployed` : 'Not deployed'} · {formatRelative(agent.updated_at)}</small></div><ChevronRight size={16} /></button>)}
        {!agents.length && <div className="empty-list">Create an agent to begin.</div>}
      </div></aside>

      <section className="builder-panel panel">{selected ? <>
        <div className="builder-toolbar"><div className="builder-title"><div className="agent-logo large"><Bot size={20} /></div><div><div className="title-line"><h3>{selected.name}</h3><StatusPill status={dirty ? 'UNSAVED' : selected.status} /></div><span>{shortId(selected.id)} · draft version {selected.version}</span></div></div><div className="builder-actions"><button className="button secondary" disabled={!!busy} onClick={() => void validate()}>{busy === 'validate' ? <LoaderCircle className="spin" size={15} /> : <CircleCheck size={15} />} Validate</button><button className="button primary" disabled={!!busy} onClick={() => void reviewPublish()}><Rocket size={15} /> Review & publish</button></div></div>
        {notice && <div className="success-banner"><Check size={16} />{notice}</div>}
        {issues.length > 0 && <div className={`validation-banner ${issues.some((issue) => issue.level === 'error') ? 'has-errors' : ''}`}><AlertTriangle size={18} /><div><strong>Validation results</strong>{issues.map((issue, index) => <span key={`${issue.message}-${index}`}>{issue.level}: {issue.message}</span>)}</div></div>}
        <div className="builder-tabs" role="tablist">{tabs.map(({ id, label, icon: Icon }) => <button role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)} key={id}><Icon size={15} />{label}</button>)}</div>
        <div className="builder-content">
          {tab === 'basics' && <div className="form-stack"><div className="form-row"><label>Agent name<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>Harness<select value={draft.harness_type} onChange={(event) => setDraft({ ...draft, harness_type: event.target.value as AgentDraft['harness_type'] })}><option value="deepagents">Deep Agents</option><option value="langchain_agent">LangChain Agent</option><option value="custom_langgraph">Custom LangGraph</option></select></label></div><label>Description<input value={description} onChange={(event) => setDescription(event.target.value)} placeholder="What this agent is responsible for" /></label><label>System prompt<div className="code-field"><div className="code-field-head"><span><FileCode2 size={14} /> Editable draft</span><small>{draft.system_prompt.length} characters</small></div><textarea rows={12} value={draft.system_prompt} onChange={(event) => setDraft({ ...draft, system_prompt: event.target.value })} /></div></label></div>}
          {tab === 'model' && <div className="model-selection"><div className="section-heading"><div><h3>Select a model deployment</h3><p>Agents bind to a concrete deployment, not a mutable provider alias.</p></div></div>{models.map((model) => <label className={`model-choice ${draft.model_deployment_id === model.id ? 'selected' : ''}`} key={model.id}><input type="radio" name="model" value={model.id} checked={draft.model_deployment_id === model.id} disabled={model.status !== 'healthy'} onChange={() => setDraft({ ...draft, model_deployment_id: model.id })} /><div className="model-logo"><Sparkles size={19} /></div><div><strong>{model.name}</strong><span>{model.provider} · {model.model}</span><small>{model.endpoint_region} · ${model.pricing.input_per_million}/1M input</small><div className="model-capabilities">{model.capabilities.map((capability) => <i key={capability}>{capability.replaceAll('_', ' ')}</i>)}</div></div><StatusPill status={model.status} /></label>)}</div>}
          {tab === 'capabilities' && <div className="capabilities-grid">
            <CapabilityPicker icon={Wrench} title="Tools" description="Approved tool gateway bindings" items={draft.capabilities.tools} onChange={(items) => updateCapabilities('tools', items)} options={[{ value: 'knowledge_search', label: 'Knowledge search', detail: 'Read-only governed retrieval', status: 'available' }, { value: 'artifact_write', label: 'Artifact write', detail: 'Run-scoped object output', status: 'approval aware' }]} />
            <CapabilityPicker icon={Bot} title="SubAgents" description="Synchronous execution spans" items={draft.capabilities.subagents} onChange={(items) => updateCapabilities('subagents', items)} options={[{ value: 'researcher', label: 'Researcher', detail: 'Evidence gathering child agent' }, { value: 'reviewer', label: 'Reviewer', detail: 'Independent result review' }]} />
            <CapabilityPicker icon={Layers3} title="Skills" description="Version-locked instruction artifacts" items={draft.capabilities.skills} onChange={(items) => updateCapabilities('skills', items)} options={skills.map((skill) => ({ value: skill.slug, label: skill.name, detail: `v${skill.version} · ${skill.plugin_name}`, status: skill.status }))} />
            <CapabilityPicker icon={Database} title="Knowledge" description="Active immutable retrieval revisions" items={draft.capabilities.knowledge_bases} onChange={(items) => updateCapabilities('knowledge_bases', items)} options={knowledgeOptions} emptyAction={<Link to="/knowledge" className="text-link">Create a knowledge base</Link>} />
            <div className="capability-card toggle-card"><div className="capability-head"><div className="capability-icon"><Code2 size={18} /></div><div><h4>Filesystem workspace</h4><p>Expose run-scoped virtual mounts</p></div><button aria-label={`${draft.capabilities.filesystem ? 'Disable' : 'Enable'} filesystem workspace`} className={`toggle ${draft.capabilities.filesystem ? 'on' : ''}`} onClick={() => updateCapabilities('filesystem', !draft.capabilities.filesystem)}><span /></button></div><div className="mount-preview"><code>/workspace</code><span>run-scoped · read/write</span><code>/artifacts</code><span>object storage · write</span></div></div>
            <div className="capability-card unavailable-card"><div className="capability-head"><div className="capability-icon"><Box size={18} /></div><div><h4>MCP servers</h4><p>No registered server revisions are available.</p></div><StatusPill status="UNAVAILABLE" /></div></div>
          </div>}
          {tab === 'guardrails' && <div className="form-stack"><div className="policy-callout"><ShieldCheck size={20} /><div><strong>Enforcement remains outside the model boundary</strong><span>Authorization, approval, credentials, execution, redaction, and audit are applied by the platform.</span></div></div><div className="form-row"><label>Permission policy<input value={draft.policies.permission_policy} onChange={(event) => setDraft({ ...draft, policies: { ...draft.policies, permission_policy: event.target.value } })} /></label><label>Approval mode<select value={draft.policies.approval_mode} onChange={(event) => setDraft({ ...draft, policies: { ...draft.policies, approval_mode: event.target.value as AgentDraft['policies']['approval_mode'] } })}><option value="high_risk">High-risk actions</option><option value="always">Every tool call</option><option value="never">Never approve risky calls</option></select></label></div><div className="limits-grid">{([['max_duration_seconds', 'Max duration', 'seconds'], ['max_model_calls', 'Model calls', 'calls'], ['max_tool_calls', 'Tool calls', 'calls'], ['max_subagent_concurrency', 'SubAgent concurrency', 'workers'], ['max_subagent_depth', 'SubAgent depth', 'levels'], ['max_cost', 'Maximum cost', 'USD']] as const).map(([key, fieldLabel, suffix]) => <label key={key}>{fieldLabel}<div className="number-field"><input type="number" value={draft.limits[key] ?? 0} onChange={(event) => setDraft({ ...draft, limits: { ...draft.limits, [key]: Number(event.target.value) } })} /><span>{suffix}</span></div></label>)}</div></div>}
          {tab === 'review' && <ReviewPanel selected={selected} draft={draft} name={name} description={description} changedSections={changedSections} models={models} onValidate={() => void validate()} onPublish={() => void reviewPublish()} />}
          {tab === 'release' && <ReleaseHistory revisions={selected.revisions ?? []} deployments={selected.deployments ?? []} />}
        </div>
        {dirty && <div className="sticky-save-bar"><div><strong>Unsaved changes</strong><span>Save this draft before validation or publishing.</span></div><button className="button ghost" onClick={discard}>Discard</button><button className="button primary" disabled={!!busy} onClick={() => void save()}>{busy === 'save' && <LoaderCircle size={15} className="spin" />}<Save size={15} /> Save draft</button></div>}
      </> : <div className="empty-release"><Bot size={28} /><h4>Select an agent</h4><p>Choose an agent draft to start editing.</p></div>}</section>
    </div>

    {creating && <div className="modal-backdrop" onMouseDown={() => setCreating(false)}><div className="modal" role="dialog" aria-modal="true" aria-label="Create an agent" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><span className="page-eyebrow">NEW DRAFT</span><h3>Create an agent</h3><p>Start with the governed Deep Agents profile.</p></div><button aria-label="Close" className="icon-button" onClick={() => setCreating(false)}><X size={18} /></button></div><div className="form-stack"><label>Agent name<input autoFocus value={createName} onChange={(event) => setCreateName(event.target.value)} placeholder="e.g. Incident investigator" /></label><label>Description<textarea rows={3} value={createDescription} onChange={(event) => setCreateDescription(event.target.value)} placeholder="What should this agent own?" /></label><div className="template-card"><div className="agent-logo"><Sparkles size={16} /></div><div><strong>Deep Agents starter</strong><span>Planning, filesystem, research SubAgent, HITL, and strict audit enabled.</span></div><Check size={16} /></div></div><div className="modal-actions"><button className="button secondary" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" disabled={!createName.trim() || !!busy} onClick={() => void create()}>{busy === 'create' && <LoaderCircle size={15} className="spin" />}Create draft</button></div></div></div>}
    {publishing && selected && <PublishDialog result={publishResult} changedSections={changedSections} environment={publishEnvironment} busy={busy} onEnvironment={setPublishEnvironment} onPublish={() => void publishRevision()} onDeploy={() => void deployRevision()} onClose={() => setPublishing(false)} />}
  </div>
}

function CapabilityPicker({ icon: Icon, title, description, items, onChange, options, emptyAction }: { icon: typeof Wrench; title: string; description: string; items: string[]; onChange: (items: string[]) => void; options: BindingOption[]; emptyAction?: React.ReactNode }) {
  const available = options.filter((option) => !items.includes(option.value))
  const add = (value: string) => { if (value) onChange([...items, value]) }
  return <div className="capability-card"><div className="capability-head"><div className="capability-icon"><Icon size={18} /></div><div><h4>{title}</h4><p>{description}</p></div><span className="count-badge">{items.length}</span></div><div className="binding-list">{items.map((item) => { const option = options.find((entry) => entry.value === item); return <div className="binding-row" key={item}><div><strong>{option?.label ?? item}</strong><span>{option?.detail ?? 'Previously bound resource'}</span></div>{option?.status && <StatusPill status={option.status} />}<button aria-label={`Remove ${option?.label ?? item}`} onClick={() => onChange(items.filter((entry) => entry !== item))}><X size={15} /></button></div> })}{!items.length && <div className="binding-empty">No bindings selected.</div>}</div>{available.length ? <label className="binding-add"><span>Add approved binding</span><select value="" onChange={(event) => add(event.target.value)}><option value="">Select a registered resource…</option>{available.map((option) => <option value={option.value} key={option.value}>{option.label} · {option.detail}</option>)}</select></label> : emptyAction ? <div className="binding-action">{emptyAction}</div> : <div className="binding-complete">All available bindings are selected.</div>}</div>
}

function ReviewPanel({ selected, draft, name, description, changedSections, models, onValidate, onPublish }: { selected: Agent; draft: AgentDraft; name: string; description: string; changedSections: string[]; models: ModelDeployment[]; onValidate: () => void; onPublish: () => void }) {
  const model = models.find((item) => item.id === draft.model_deployment_id)
  return <div className="review-panel"><div className="review-summary"><div><span>Draft</span><strong>v{selected.version}</strong></div><div><span>Model</span><strong>{model?.name ?? draft.model_deployment_id}</strong></div><div><span>Bindings</span><strong>{draft.capabilities.tools.length + draft.capabilities.skills.length + draft.capabilities.knowledge_bases.length}</strong></div><div><span>Max cost</span><strong>${draft.limits.max_cost ?? '—'}</strong></div></div><div className="review-section"><h4>Changes since the latest revision</h4>{changedSections.length ? <ul>{changedSections.map((section) => <li key={section}><Check size={15} />{section}</li>)}</ul> : <p>No configuration changes. Publishing now would create an equivalent revision.</p>}</div><div className="review-section"><h4>Release identity</h4><dl><div><dt>Name</dt><dd>{name}</dd></div><div><dt>Description</dt><dd>{description || 'No description'}</dd></div><div><dt>Approval mode</dt><dd>{draft.policies.approval_mode.replaceAll('_', ' ')}</dd></div></dl></div><div className="review-actions"><button className="button secondary" onClick={onValidate}><CircleCheck size={16} /> Validate draft</button><button className="button primary" onClick={onPublish}><Rocket size={16} /> Review publication</button></div></div>
}

function ReleaseHistory({ revisions, deployments }: { revisions: Revision[]; deployments: Deployment[] }) {
  return <div className="release-list">{revisions.length ? revisions.map((revision, index) => { const deployment = deployments.find((item) => item.agent_revision_id === revision.id); return <div className="release-item" key={revision.id}><div className="release-rail"><span><GitBranch size={15} /></span>{index < revisions.length - 1 && <i />}</div><div className="release-copy"><div><strong>Revision {revision.revision_number}</strong><StatusPill status={index === 0 ? 'CURRENT' : 'IMMUTABLE'} />{deployment && <StatusPill status={deployment.environment} />}</div><span>{shortId(revision.id)} · {new Date(revision.created_at).toLocaleString()}</span><p>{revision.spec.harness_type} · {revision.spec.model_deployment_id} · {revision.spec.capabilities.tools.length} tools</p></div><button className="icon-button" aria-label={`Copy revision ${revision.revision_number} ID`} onClick={() => navigator.clipboard.writeText(revision.id)}><Copy size={15} /></button></div> }) : <div className="empty-release"><GitBranch size={24} /><h4>No immutable revisions yet</h4><p>Validate and publish this draft to create revision 1.</p></div>}</div>
}

function PublishDialog({ result, changedSections, environment, busy, onEnvironment, onPublish, onDeploy, onClose }: { result: PublishResult | null; changedSections: string[]; environment: string; busy: string; onEnvironment: (value: string) => void; onPublish: () => void; onDeploy: () => void; onClose: () => void }) {
  return <div className="modal-backdrop"><div className="modal publish-modal" role="dialog" aria-modal="true" aria-label="Publish agent revision"><div className="modal-heading"><div><span className="page-eyebrow">IMMUTABLE RELEASE</span><h3>{result ? `Revision ${result.revision.revision_number} published` : 'Review publication'}</h3><p>{result ? 'The revision is immutable. Deployment remains an explicit step.' : 'Confirm the changes that will be locked into this revision.'}</p></div><button aria-label="Close" className="icon-button" onClick={onClose}><X size={18} /></button></div>{!result ? <><div className="publish-review"><h4>Changed sections</h4>{changedSections.length ? changedSections.map((section) => <div key={section}><Check size={16} />{section}</div>) : <p>No differences from the latest revision.</p>}<div className="policy-callout"><ShieldCheck size={20} /><div><strong>Publishing does not deploy automatically</strong><span>You will choose an environment after the revision and plan are created.</span></div></div></div><div className="modal-actions"><button className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={!!busy} onClick={onPublish}>{busy === 'publish' && <LoaderCircle className="spin" size={15} />} Publish revision</button></div></> : <><div className="publish-result"><div><span>Revision</span><code>{result.revision.id}</code></div><div><span>Resolved plan</span><code>{result.resolved_plan.id}</code></div><div><span>Plan hash</span><code>{result.resolved_plan.plan_hash}</code></div>{result.deployment ? <div className="deployment-success"><Check size={18} /><div><strong>Deployed to {result.deployment.environment}</strong><span>{result.deployment.id}</span></div></div> : <label>Deployment target<select value={environment} onChange={(event) => onEnvironment(event.target.value)}><option value="development">Development</option><option value="staging">Staging</option><option value="production">Production</option></select><span className="field-hint">Production runs may trigger additional policy approval.</span></label>}</div><div className="modal-actions"><button className="button secondary" onClick={onClose}>{result.deployment ? 'Done' : 'Deploy later'}</button>{!result.deployment && <button className="button primary" disabled={!!busy} onClick={onDeploy}>{busy === 'deploy' && <LoaderCircle className="spin" size={15} />} Deploy revision</button>}</div></>}</div></div>
}

function getChangedSections(latest: Revision | undefined, draft: AgentDraft, name: string, description: string, agent: Agent) {
  if (!latest) return ['Initial revision', 'Identity', 'Model', 'Capabilities', 'Guardrails']
  const changes: string[] = []
  if (name !== agent.name || description !== agent.description) changes.push('Identity')
  if (latest.spec.model_deployment_id !== draft.model_deployment_id) changes.push('Model deployment')
  if (latest.spec.system_prompt !== draft.system_prompt) changes.push('System prompt')
  if (JSON.stringify(latest.spec.capabilities) !== JSON.stringify(draft.capabilities)) changes.push('Capability bindings')
  if (JSON.stringify(latest.spec.policies) !== JSON.stringify(draft.policies)) changes.push('Guardrails')
  if (JSON.stringify(latest.spec.limits) !== JSON.stringify(draft.limits)) changes.push('Execution limits')
  return changes
}
