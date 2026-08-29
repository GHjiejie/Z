import {
  AlertTriangle,
  Bot,
  Box,
  Check,
  ChevronRight,
  CircleCheck,
  Code2,
  Copy,
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
import { api } from '../lib/api'
import type { Agent, AgentDraft, ModelDeployment, Skill } from '../types'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'

const defaultDraft: AgentDraft = {
  harness_type: 'deepagents',
  harness_profile_revision_id: 'deepagents-0.x-adapter-1.0',
  model_deployment_id: 'model_qwen_prod_v1',
  system_prompt: 'You are a careful project agent. Plan first, use approved tools, and cite artifacts.',
  capabilities: { tools: ['knowledge_search', 'artifact_write'], mcp_servers: [], skills: ['task-planning', 'release-safety'], memories: [], knowledge_bases: [], subagents: ['researcher'], filesystem: true },
  policies: { permission_policy: 'project-default', approval_mode: 'high_risk', audit_level: 'strict' },
  limits: { max_duration_seconds: 600, max_model_calls: 20, max_tool_calls: 30, max_subagent_depth: 3, max_subagent_concurrency: 4, max_sandbox_cpu_seconds: 120, max_output_bytes: 1000000, max_cost: 5 },
  output_schema: null,
}

type BuilderTab = 'identity' | 'capabilities' | 'policies' | 'release'

export function AgentsPage() {
  const [agents, setAgents] = useState<Agent[]>([])
  const [models, setModels] = useState<ModelDeployment[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [selected, setSelected] = useState<Agent | null>(null)
  const [draft, setDraft] = useState<AgentDraft>(defaultDraft)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [tab, setTab] = useState<BuilderTab>('identity')
  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)

  const load = async () => {
    try {
      const [agentResult, modelResult, skillResult] = await Promise.all([api.agents(), api.models(), api.skills()])
      setAgents(agentResult.items)
      setModels(modelResult.items)
      setSkills(skillResult.items)
      if (!selected && agentResult.items[0]) await selectAgent(agentResult.items[0].id)
      setLoaded(true)
    } catch (err) { setError((err as Error).message); setLoaded(true) }
  }

  useEffect(() => { void load() }, [])

  const selectAgent = async (id: string) => {
    try {
      const agent = await api.agent(id)
      setSelected(agent); setDraft(structuredClone(agent.draft)); setName(agent.name); setDescription(agent.description); setNotice('')
    } catch (err) { setError((err as Error).message) }
  }

  const dirty = useMemo(() => selected && (name !== selected.name || description !== selected.description || JSON.stringify(draft) !== JSON.stringify(selected.draft)), [selected, name, description, draft])

  const save = async () => {
    if (!selected) return
    setBusy('save'); setError(''); setNotice('')
    try {
      const updated = await api.updateAgent(selected.id, { name, description, draft, version: selected.version })
      setSelected(updated); setDraft(structuredClone(updated.draft)); setNotice('Draft saved as a new editable version.'); await load()
    } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }

  const validate = async () => {
    if (!selected) return
    if (dirty) await save()
    setBusy('validate'); setError(''); setNotice('')
    try {
      const result = await api.validateAgent(selected.id)
      setNotice(result.valid ? `Validation passed${result.issues.length ? ` with ${result.issues.length} warning` : ''}.` : 'Validation found blocking errors.')
    } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }

  const publish = async () => {
    if (!selected) return
    setBusy('publish'); setError(''); setNotice('')
    try {
      if (dirty) await save()
      const result = await api.publishAgent(selected.id)
      const deployment = await api.deploy(result.revision.id)
      setNotice(`Revision ${result.revision.revision_number} published and deployed to ${deployment.environment}.`)
      await load(); await selectAgent(selected.id)
    } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }

  const create = async () => {
    if (!name.trim()) return
    setBusy('create')
    try {
      const agent = await api.createAgent({ name, description, draft: defaultDraft })
      setCreating(false); await load(); await selectAgent(agent.id)
    } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }

  const startCreate = () => { setCreating(true); setName(''); setDescription(''); setDraft(defaultDraft) }
  const updateCapabilities = (key: keyof AgentDraft['capabilities'], value: any) => setDraft({ ...draft, capabilities: { ...draft.capabilities, [key]: value } })

  if (!loaded) return <LoadingBlock />

  return (
    <div className="page-stack">
      <PageHeader eyebrow="AGENT REGISTRY" title="Build, version, and deploy agents" description="Edit drafts freely. Every publish creates an immutable revision and a dependency-locked execution plan." actions={<button className="button primary" onClick={startCreate}><Plus size={16} /> New agent</button>} />
      {error && <ErrorBanner message={error} />}
      <div className="builder-layout">
        <aside className="agent-list-panel panel">
          <div className="list-search"><Bot size={16} /><span>{agents.length} registered agents</span></div>
          <div className="agent-list">
            {agents.map((agent) => (
              <button key={agent.id} className={`agent-list-item ${selected?.id === agent.id ? 'selected' : ''}`} onClick={() => void selectAgent(agent.id)}>
                <div className="agent-logo"><Sparkles size={17} /></div>
                <div className="agent-list-copy"><strong>{agent.name}</strong><span>{agent.description || 'No description'}</span><small>v{agent.revision_count || 0} · {formatRelative(agent.updated_at)}</small></div>
                <ChevronRight size={16} />
              </button>
            ))}
          </div>
        </aside>

        <section className="builder-panel panel">
          {selected ? <>
            <div className="builder-toolbar">
              <div className="builder-title"><div className="agent-logo large"><Bot size={20} /></div><div><div className="title-line"><h3>{selected.name}</h3><StatusPill status={dirty ? 'UNSAVED' : selected.status} /></div><span>{shortId(selected.id)} · draft version {selected.version}</span></div></div>
              <div className="builder-actions"><button className="button ghost" disabled={!!busy} onClick={() => void validate()}>{busy === 'validate' ? <LoaderCircle className="spin" size={15} /> : <CircleCheck size={15} />} Validate</button><button className="button secondary" disabled={!dirty || !!busy} onClick={() => void save()}>{busy === 'save' ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} Save draft</button><button className="button primary" disabled={!!busy} onClick={() => void publish()}>{busy === 'publish' ? <LoaderCircle className="spin" size={15} /> : <Rocket size={15} />} Publish</button></div>
            </div>
            {notice && <div className="success-banner"><Check size={16} />{notice}</div>}
            <div className="builder-tabs">
              <button className={tab === 'identity' ? 'active' : ''} onClick={() => setTab('identity')}><Settings2 size={15} /> Identity & model</button>
              <button className={tab === 'capabilities' ? 'active' : ''} onClick={() => setTab('capabilities')}><Wrench size={15} /> Capabilities</button>
              <button className={tab === 'policies' ? 'active' : ''} onClick={() => setTab('policies')}><ShieldCheck size={15} /> Policy & limits</button>
              <button className={tab === 'release' ? 'active' : ''} onClick={() => setTab('release')}><GitBranch size={15} /> Release history</button>
            </div>
            <div className="builder-content">
              {tab === 'identity' && <div className="form-stack">
                <div className="form-row"><label>Agent name<input value={name} onChange={(e) => setName(e.target.value)} /></label><label>Harness<select value={draft.harness_type} onChange={(e) => setDraft({ ...draft, harness_type: e.target.value as AgentDraft['harness_type'] })}><option value="deepagents">Deep Agents</option><option value="langchain_agent">LangChain Agent</option><option value="custom_langgraph">Custom LangGraph</option></select></label></div>
                <label>Description<input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What this agent is responsible for" /></label>
                <label>Model deployment<select value={draft.model_deployment_id} onChange={(e) => setDraft({ ...draft, model_deployment_id: e.target.value })}>{models.map((model) => <option value={model.id} key={model.id}>{model.name} · {model.model} · {model.endpoint_region}</option>)}</select><span className="field-hint">Agents bind to a model deployment revision, never a mutable provider alias.</span></label>
                <label>System prompt<div className="code-field"><div className="code-field-head"><span><FileCode2 size={14} /> Prompt revision</span><small>{draft.system_prompt.length} chars</small></div><textarea rows={10} value={draft.system_prompt} onChange={(e) => setDraft({ ...draft, system_prompt: e.target.value })} /></div></label>
              </div>}
              {tab === 'capabilities' && <div className="capabilities-grid">
                <CapabilityCard icon={Wrench} title="Tools" description="Runtime tool gateway bindings" items={draft.capabilities.tools} onChange={(items) => updateCapabilities('tools', items)} suggestions={['knowledge_search', 'artifact_write', 'shell_execute']} />
                <CapabilityCard icon={Bot} title="SubAgents" description="Synchronous execution spans" items={draft.capabilities.subagents} onChange={(items) => updateCapabilities('subagents', items)} suggestions={['researcher', 'reviewer', 'coder']} />
                <CapabilityCard icon={Layers3} title="Skills" description="Versioned instructions loaded by the harness" items={draft.capabilities.skills} onChange={(items) => updateCapabilities('skills', items)} suggestions={skills.map((skill) => skill.slug)} />
                <CapabilityCard icon={Box} title="MCP servers" description="Versioned discovery snapshots" items={draft.capabilities.mcp_servers} onChange={(items) => updateCapabilities('mcp_servers', items)} suggestions={['github-mcp-v1', 'postgres-readonly-v1']} />
                <div className="capability-card toggle-card"><div className="capability-head"><div className="capability-icon"><Code2 size={18} /></div><div><h4>Filesystem workspace</h4><p>Expose virtual workspace mounts</p></div><button className={`toggle ${draft.capabilities.filesystem ? 'on' : ''}`} onClick={() => updateCapabilities('filesystem', !draft.capabilities.filesystem)}><span /></button></div><div className="mount-preview"><code>/workspace</code><span>run-scoped · read/write</span><code>/artifacts</code><span>object storage · write</span></div></div>
              </div>}
              {tab === 'policies' && <div className="form-stack">
                <div className="policy-callout"><ShieldCheck size={20} /><div><strong>Policy enforcement is outside the model boundary</strong><span>Visibility → authorization → risk → approval → credential → execution → redaction → audit</span></div></div>
                <div className="form-row"><label>Permission policy<input value={draft.policies.permission_policy} onChange={(e) => setDraft({ ...draft, policies: { ...draft.policies, permission_policy: e.target.value } })} /></label><label>Approval mode<select value={draft.policies.approval_mode} onChange={(e) => setDraft({ ...draft, policies: { ...draft.policies, approval_mode: e.target.value as AgentDraft['policies']['approval_mode'] } })}><option value="high_risk">High-risk actions</option><option value="always">Every tool call</option><option value="never">Never (deny risky calls)</option></select></label></div>
                <div className="limits-grid">
                  {([['max_duration_seconds', 'Max duration', 'seconds'], ['max_model_calls', 'Model calls', 'calls'], ['max_tool_calls', 'Tool calls', 'calls'], ['max_subagent_concurrency', 'SubAgent concurrency', 'workers'], ['max_subagent_depth', 'SubAgent depth', 'levels'], ['max_cost', 'Maximum cost', 'USD']] as const).map(([key, label, suffix]) => <label key={key}>{label}<div className="number-field"><input type="number" value={draft.limits[key] ?? 0} onChange={(e) => setDraft({ ...draft, limits: { ...draft.limits, [key]: Number(e.target.value) } })} /><span>{suffix}</span></div></label>)}
                </div>
              </div>}
              {tab === 'release' && <div className="release-list">
                {selected.revisions?.length ? selected.revisions.map((revision, index) => <div className="release-item" key={revision.id}><div className="release-rail"><span><GitBranch size={15} /></span>{index < selected.revisions!.length - 1 && <i />}</div><div className="release-copy"><div><strong>Revision {revision.revision_number}</strong><StatusPill status={index === 0 ? 'CURRENT' : 'IMMUTABLE'} /></div><span>{shortId(revision.id)} · {new Date(revision.created_at).toLocaleString()}</span><p>{revision.spec.harness_type} · {revision.spec.model_deployment_id} · {revision.spec.capabilities.tools.length} tools</p></div><button className="icon-button" title="Copy revision ID" onClick={() => navigator.clipboard.writeText(revision.id)}><Copy size={15} /></button></div>) : <div className="empty-release"><GitBranch size={24} /><h4>No immutable revisions yet</h4><p>Validate and publish this draft to create revision 1.</p></div>}
              </div>}
            </div>
          </> : <div className="empty-release"><Bot size={28} /><h4>Select an agent</h4><p>Choose an agent draft to start editing.</p></div>}
        </section>
      </div>

      {creating && <div className="modal-backdrop" onMouseDown={() => setCreating(false)}><div className="modal" onMouseDown={(e) => e.stopPropagation()}><div className="modal-heading"><div><span className="page-eyebrow">NEW DRAFT</span><h3>Create an agent</h3><p>Start with the reviewed Deep Agents harness profile.</p></div><button className="icon-button" onClick={() => setCreating(false)}><X size={18} /></button></div><div className="form-stack"><label>Agent name<input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Incident investigator" /></label><label>Description<textarea rows={3} value={description} onChange={(e) => setDescription(e.target.value)} placeholder="What should this agent own?" /></label><div className="template-card"><div className="agent-logo"><Sparkles size={16} /></div><div><strong>Deep Agents starter</strong><span>Planning, filesystem, research SubAgent, HITL, and strict audit enabled.</span></div><Check size={16} /></div></div><div className="modal-actions"><button className="button secondary" onClick={() => setCreating(false)}>Cancel</button><button className="button primary" disabled={!name.trim() || !!busy} onClick={() => void create()}>{busy === 'create' && <LoaderCircle size={15} className="spin" />} Create draft</button></div></div></div>}
    </div>
  )
}

function CapabilityCard({ icon: Icon, title, description, items, onChange, suggestions }: { icon: typeof Wrench; title: string; description: string; items: string[]; onChange: (items: string[]) => void; suggestions: string[] }) {
  const [value, setValue] = useState('')
  const add = () => { const item = value.trim(); if (item && !items.includes(item)) onChange([...items, item]); setValue('') }
  return <div className="capability-card"><div className="capability-head"><div className="capability-icon"><Icon size={18} /></div><div><h4>{title}</h4><p>{description}</p></div><span className="count-badge">{items.length}</span></div><div className="chips">{items.map((item) => <span className="chip" key={item}>{item}<button onClick={() => onChange(items.filter((entry) => entry !== item))}><X size={12} /></button></span>)}</div><div className="inline-add"><input list={`${title}-suggestions`} value={value} onChange={(e) => setValue(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }} placeholder={`Add ${title.toLowerCase()} binding`} /><datalist id={`${title}-suggestions`}>{suggestions.map((item) => <option key={item} value={item} />)}</datalist><button onClick={add}><Plus size={15} /></button></div></div>
}
