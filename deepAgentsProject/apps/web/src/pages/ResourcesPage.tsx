import {
  ArrowUpRight,
  Bot,
  Boxes,
  BrainCircuit,
  CheckCircle2,
  ChevronRight,
  CloudCog,
  Database,
  Gauge,
  Globe2,
  HardDrive,
  KeyRound,
  Layers3,
  Network,
  Plus,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  Wrench,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill } from '../components/UI'
import { api } from '../lib/api'
import type { ModelDeployment, Plugin, Skill } from '../types'

const baseRegistries = [
  { icon: Wrench, name: 'Tool registry', count: 2, description: 'HTTP, internal API, database, and remote tools', tone: 'amber' },
  { icon: Network, name: 'MCP registry', count: 0, description: 'Versioned server definitions and discovery snapshots', tone: 'blue' },
  { icon: BrainCircuit, name: 'Memory sources', count: 0, description: 'User and project-scoped durable context', tone: 'rose' },
  { icon: Database, name: 'Knowledge bases', count: 0, description: 'Indexed corpora exposed through retriever tools', tone: 'cyan' },
  { icon: HardDrive, name: 'Workspace profiles', count: 1, description: 'Virtual mounts, scopes, quotas, and retention', tone: 'green' },
]

export function ResourcesPage() {
  const [models, setModels] = useState<ModelDeployment[]>([])
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [knowledgeCount, setKnowledgeCount] = useState(0)
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  useEffect(() => { Promise.all([api.models(), api.plugins(), api.skills(), api.knowledgeBases()]).then(([modelResult, pluginResult, skillResult, knowledgeResult]) => { setModels(modelResult.items); setPlugins(pluginResult.items); setSkills(skillResult.items); setKnowledgeCount(knowledgeResult.items.length); setLoaded(true) }).catch((err) => { setError(err.message); setLoaded(true) }) }, [])
  if (!loaded) return <LoadingBlock />
  const registries = [
    ...baseRegistries.slice(0, 2),
    { icon: Layers3, name: 'Skill registry', count: skills.length, description: 'Version-locked progressive instruction artifacts', tone: 'violet' },
    ...baseRegistries.slice(2).map((registry) => registry.name === 'Knowledge bases' ? { ...registry, count: knowledgeCount } : registry),
  ]
  return <div className="page-stack">
    <PageHeader eyebrow="CONTROL PLANE" title="Govern every runtime dependency" description="Resources keep independent control-plane lifecycles and converge only through immutable capability bindings." actions={<button className="button primary"><Plus size={16} /> Register resource</button>} />
    {error && <ErrorBanner message={error} />}
    <section className="panel models-panel"><div className="panel-heading"><div><h3>Model deployments</h3><p>Provider endpoints, verified capabilities, routing health, and pricing</p></div><button className="button ghost">Manage routing <ArrowUpRight size={14} /></button></div><div className="model-grid">{models.map((model) => <div className="model-card" key={model.id}><div className="model-head"><div className="model-logo"><Sparkles size={20} /></div><div><h4>{model.name}</h4><span>{model.provider}</span></div><StatusPill status={model.status} /></div><div className="model-name"><span>MODEL</span><code>{model.model}</code></div><div className="model-capabilities">{model.capabilities.map((capability) => <span key={capability}><CheckCircle2 size={12} />{capability.replaceAll('_', ' ')}</span>)}</div><div className="model-footer"><span><Globe2 size={13} />{model.endpoint_region}</span><span>${model.pricing.input_per_million} / 1M in</span><ChevronRight size={15} /></div></div>)}</div></section>
    <section><div className="section-heading"><div><h3>Capability registries</h3><p>Versioned resources available to agent revisions</p></div><div className="table-search compact"><Search size={15} /><input placeholder="Find a registry…" /></div></div><div className="registry-grid">{registries.map(({ icon: Icon, name, count, description, tone }) => <button className="registry-card" key={name}><div className={`registry-icon tone-${tone}`}><Icon size={19} /></div><div><div><h4>{name}</h4><span>{count}</span></div><p>{description}</p></div><ChevronRight size={16} /></button>)}</div></section>
    <section className="panel skill-catalog"><div className="panel-heading"><div><h3>Built-in skill plugins</h3><p>Discovered at API startup, registered idempotently, and pinned into every published execution plan</p></div><span className="plugin-count"><Boxes size={14} />{plugins.length} plugin{plugins.length === 1 ? '' : 's'}</span></div><div className="skill-grid">{skills.map((skill) => <div className="skill-card" key={skill.id}><div className="skill-head"><div className="registry-icon tone-violet"><Layers3 size={17} /></div><div><h4>{skill.name}</h4><span>{skill.plugin_name} · v{skill.version}</span></div><StatusPill status={skill.builtin ? 'BUILT-IN' : skill.status} /></div><p>{skill.description}</p><div className="skill-tags">{skill.tags.map((tag) => <span key={tag}>{tag}</span>)}</div><code title={skill.artifact_hash}>sha256:{skill.artifact_hash.slice(0, 12)}</code></div>)}</div></section>
    <section className="security-foundation"><div className="security-copy"><div className="section-kicker"><ShieldCheck size={13} /> Security foundation</div><h3>Credentials never enter the execution plan</h3><p>Workers receive opaque credential handles. The broker resolves short-lived secrets immediately before a permitted tool call.</p><button className="text-link">View policy architecture <ArrowUpRight size={14} /></button></div><div className="security-flow"><div><div className="security-node"><Bot size={18} /></div><span>Agent call</span></div><i /><div><div className="security-node"><ShieldCheck size={18} /></div><span>Policy gateway</span></div><i /><div><div className="security-node active"><KeyRound size={18} /></div><span>Credential broker</span></div><i /><div><div className="security-node"><CloudCog size={18} /></div><span>Bound resource</span></div></div></section>
  </div>
}
