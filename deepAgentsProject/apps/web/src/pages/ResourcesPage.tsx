import {
  Boxes,
  CheckCircle2,
  ChevronRight,
  Database,
  Globe2,
  Layers3,
  Search,
  Server,
  ShieldCheck,
  Sparkles,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill } from '../components/UI'
import { usePlatform } from '../context/PlatformContext'
import { api } from '../lib/api'
import type { ModelDeployment, Plugin, Skill } from '../types'

type ResourceSelection = { type: 'model'; value: ModelDeployment } | { type: 'skill'; value: Skill } | null

export function ResourcesPage() {
  const [models, setModels] = useState<ModelDeployment[]>([])
  const [plugins, setPlugins] = useState<Plugin[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [knowledgeCount, setKnowledgeCount] = useState(0)
  const [query, setQuery] = useState('')
  const [selection, setSelection] = useState<ResourceSelection>(null)
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const { context } = usePlatform()
  useEffect(() => { Promise.all([api.models(), api.plugins(), api.skills(), api.knowledgeBases()]).then(([modelResult, pluginResult, skillResult, knowledgeResult]) => { setModels(modelResult.items); setPlugins(pluginResult.items); setSkills(skillResult.items); setKnowledgeCount(knowledgeResult.items.length) }).catch((nextError) => setError(nextError.message)).finally(() => setLoaded(true)) }, [])
  const needle = query.trim().toLowerCase()
  const visibleModels = models.filter((model) => `${model.name} ${model.model} ${model.provider} ${model.endpoint_region}`.toLowerCase().includes(needle))
  const visibleSkills = skills.filter((skill) => `${skill.name} ${skill.slug} ${skill.description} ${skill.tags.join(' ')}`.toLowerCase().includes(needle))
  const registries = useMemo(() => [
    { icon: Server, name: 'Model deployments', count: models.length, description: 'Provider endpoints, capability contracts, health, and pricing', tone: 'violet', anchor: 'models' },
    { icon: Layers3, name: 'Skill registry', count: skills.length, description: 'Version-locked instruction artifacts discovered from plugins', tone: 'blue', anchor: 'skills' },
    { icon: Database, name: 'Knowledge bases', count: knowledgeCount, description: 'Active corpora with immutable retrieval revisions', tone: 'cyan', to: '/knowledge' },
    { icon: Boxes, name: 'Plugin catalog', count: plugins.length, description: 'Loaded declarative plugin manifests and their skills', tone: 'green', anchor: 'plugins' },
  ], [models.length, skills.length, knowledgeCount, plugins.length])
  if (!loaded) return <LoadingBlock />

  return <div className="page-stack resources-page">
    <PageHeader eyebrow="CONTROL PLANE" title="Govern runtime dependencies" description="Only registries exposed by the current API are shown. Open a resource to inspect its health, immutable identity, and runtime contract." />
    {error && <ErrorBanner message={error} />}
    <div className="resource-search panel"><Search size={18} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search models and skills…" /><span>{visibleModels.length + visibleSkills.length} matching resources</span></div>
    <section><div className="section-heading"><div><h3>Available registries</h3><p>Counts are derived from the active project and plugin registry.</p></div></div><div className="registry-grid">{registries.map(({ icon: Icon, name, count, description, tone, anchor, to }) => to ? <Link className="registry-card" to={to} key={name}><div className={`registry-icon tone-${tone}`}><Icon size={19} /></div><div><div><h4>{name}</h4><span>{count}</span></div><p>{description}</p></div><ChevronRight size={16} /></Link> : <a className="registry-card" href={`#${anchor}`} key={name}><div className={`registry-icon tone-${tone}`}><Icon size={19} /></div><div><div><h4>{name}</h4><span>{count}</span></div><p>{description}</p></div><ChevronRight size={16} /></a>)}</div></section>

    <section id="models" className="panel models-panel"><div className="panel-heading"><div><h3>Model deployments</h3><p>Verified capability contracts and reported endpoint health</p></div><span className="count-badge">{visibleModels.length}</span></div><div className="model-grid">{visibleModels.map((model) => <button className="model-card" key={model.id} onClick={() => setSelection({ type: 'model', value: model })}><div className="model-head"><div className="model-logo"><Sparkles size={20} /></div><div><h4>{model.name}</h4><span>{model.provider}</span></div><StatusPill status={model.status} /></div><div className="model-name"><span>MODEL</span><code>{model.model}</code></div><div className="model-capabilities">{model.capabilities.map((capability) => <span key={capability}><CheckCircle2 size={12} />{capability.replaceAll('_', ' ')}</span>)}</div><div className="model-footer"><span><Globe2 size={13} />{model.endpoint_region}</span><span>${model.pricing.input_per_million} / 1M input</span><ChevronRight size={15} /></div></button>)}{!visibleModels.length && <div className="registry-empty">No model deployments match this search.</div>}</div></section>

    <section id="skills" className="panel skill-catalog"><div className="panel-heading"><div><h3>Skill registry</h3><p>Versioned artifacts pinned into published execution plans</p></div><span className="plugin-count"><Layers3 size={14} />{visibleSkills.length} skills</span></div><div className="skill-grid">{visibleSkills.map((skill) => <button className="skill-card" key={skill.id} onClick={() => setSelection({ type: 'skill', value: skill })}><div className="skill-head"><div className="registry-icon tone-violet"><Layers3 size={17} /></div><div><h4>{skill.name}</h4><span>{skill.plugin_name} · v{skill.version}</span></div><StatusPill status={skill.builtin ? 'BUILT-IN' : skill.status} /></div><p>{skill.description}</p><div className="skill-tags">{skill.tags.map((tag) => <span key={tag}>{tag}</span>)}</div><code title={skill.artifact_hash}>sha256:{skill.artifact_hash.slice(0, 12)}</code></button>)}{!visibleSkills.length && <div className="registry-empty">No skills match this search.</div>}</div></section>

    <section id="plugins" className="panel plugin-list"><div className="panel-heading"><div><h3>Loaded plugins</h3><p>Declarative packages discovered during API startup</p></div><span className="plugin-count"><Boxes size={14} />{plugins.length} loaded</span></div><div>{plugins.map((plugin) => <article key={plugin.id}><div><strong>{plugin.name}</strong><span>v{plugin.version} · {plugin.skill_count} skills</span></div><StatusPill status={plugin.status} /><p>{plugin.description}</p><code>{plugin.manifest_hash}</code></article>)}</div></section>

    <section className="security-foundation"><div className="security-copy"><div className="section-kicker"><ShieldCheck size={13} /> Security foundation</div><h3>Credentials stay outside execution plans</h3><p>Published revisions contain resource identities and policy bindings. Workers resolve short-lived credentials only immediately before permitted calls.</p></div><div className="security-note"><ShieldCheck size={21} /><div><strong>Current environment</strong><span>{context?.environment.name ?? 'Unavailable'} · {context?.runtime.status ?? 'unavailable'} runtime</span></div></div></section>
    {selection && <ResourceDetail selection={selection} onClose={() => setSelection(null)} />}
  </div>
}

function ResourceDetail({ selection, onClose }: { selection: Exclude<ResourceSelection, null>; onClose: () => void }) {
  const model = selection.type === 'model' ? selection.value : null
  const skill = selection.type === 'skill' ? selection.value : null
  return <div className="drawer-backdrop" onMouseDown={onClose}><aside className="resource-drawer" onMouseDown={(event) => event.stopPropagation()}><div className="drawer-heading"><div><span className="page-eyebrow">{selection.type.toUpperCase()} RESOURCE</span><h3>{model?.name ?? skill?.name}</h3></div><button aria-label="Close resource details" className="icon-button" onClick={onClose}><X size={18} /></button></div><div className="resource-detail-body">{model ? <><StatusPill status={model.status} /><dl><div><dt>Resource ID</dt><dd><code>{model.id}</code></dd></div><div><dt>Provider</dt><dd>{model.provider}</dd></div><div><dt>Model</dt><dd><code>{model.model}</code></dd></div><div><dt>Region</dt><dd>{model.endpoint_region}</dd></div><div><dt>Input price</dt><dd>${model.pricing.input_per_million} / 1M</dd></div><div><dt>Output price</dt><dd>${model.pricing.output_per_million} / 1M</dd></div></dl><h4>Reported capabilities</h4><div className="model-capabilities">{model.capabilities.map((capability) => <span key={capability}><CheckCircle2 size={12} />{capability.replaceAll('_', ' ')}</span>)}</div></> : skill ? <><StatusPill status={skill.builtin ? 'BUILT-IN' : skill.status} /><p>{skill.description}</p><dl><div><dt>Skill ID</dt><dd><code>{skill.id}</code></dd></div><div><dt>Slug</dt><dd><code>{skill.slug}</code></dd></div><div><dt>Plugin</dt><dd>{skill.plugin_name}</dd></div><div><dt>Version</dt><dd>{skill.version}</dd></div><div><dt>Artifact hash</dt><dd><code>{skill.artifact_hash}</code></dd></div></dl><h4>Tags</h4><div className="skill-tags">{skill.tags.map((tag) => <span key={tag}>{tag}</span>)}</div></> : null}</div></aside></div>
}
