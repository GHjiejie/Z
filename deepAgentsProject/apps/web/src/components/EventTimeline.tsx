import {
  Bot,
  Brain,
  CheckCircle2,
  CircleDot,
  Copy,
  FileText,
  Gauge,
  GitBranch,
  Hammer,
  PackageCheck,
  PauseCircle,
  Play,
  Sparkles,
  UserRound,
  Workflow,
  XCircle,
} from 'lucide-react'
import type { RuntimeEvent } from '../types'
import { formatRelative } from './UI'

export type EventCategory = 'runtime' | 'graph' | 'model' | 'tools' | 'human' | 'subagents' | 'output'

const CATEGORY_LABELS: Record<EventCategory, string> = {
  runtime: 'Runtime',
  graph: 'Graph',
  model: 'Model',
  tools: 'Tools',
  human: 'Human',
  subagents: 'SubAgents',
  output: 'Output',
}

export function eventCategoryKey(event: RuntimeEvent): EventCategory {
  const type = event.type
  if (type.startsWith('graph.')) return 'graph'
  if (
    type.startsWith('interrupt.')
    || type === 'tool.approval_required'
    || type === 'run.waiting_for_input'
    || type === 'run.input_received'
  ) return 'human'
  if (type.startsWith('model.')) return 'model'
  if (type.startsWith('tool.')) return 'tools'
  if (type.startsWith('subagent.')) return 'subagents'
  if (type.startsWith('artifact.') || type.startsWith('usage.')) return 'output'
  return 'runtime'
}

export function eventCategory(event: RuntimeEvent) {
  return CATEGORY_LABELS[eventCategoryKey(event)]
}

function eventMeta(event: RuntimeEvent) {
  const category = eventCategoryKey(event)
  if (event.type.includes('failed') || event.type.includes('cancel')) return { icon: XCircle, label: CATEGORY_LABELS[category], tone: 'rose' }
  if (event.type.startsWith('intent.')) return { icon: Brain, label: 'Intent', tone: 'violet' }
  if (event.type.startsWith('routing.')) return { icon: Workflow, label: 'Routing', tone: 'indigo' }
  if (category === 'graph') return { icon: Workflow, label: 'Graph', tone: 'indigo' }
  if (category === 'human') return { icon: UserRound, label: 'Human', tone: 'rose' }
  if (event.type.startsWith('model.reasoning.')) return { icon: Brain, label: 'Reasoning', tone: 'violet' }
  if (category === 'model') return { icon: Sparkles, label: 'Model', tone: 'violet' }
  if (category === 'tools') return { icon: Hammer, label: 'Tool', tone: 'amber' }
  if (category === 'subagents') return { icon: Bot, label: 'SubAgent', tone: 'cyan' }
  if (event.type.startsWith('artifact.')) return { icon: FileText, label: 'Artifact', tone: 'green' }
  if (event.type.startsWith('usage.')) return { icon: Gauge, label: 'Usage', tone: 'blue' }
  if (event.type.startsWith('todo.')) return { icon: CheckCircle2, label: 'Plan', tone: 'blue' }
  if (event.type.startsWith('skill.')) return { icon: PackageCheck, label: 'Skill', tone: 'cyan' }
  if (event.type.includes('started') || event.type.includes('resumed')) return { icon: Play, label: 'Runtime', tone: 'green' }
  if (event.type.includes('waiting') || event.type.includes('paused')) return { icon: PauseCircle, label: 'Runtime', tone: 'amber' }
  return { icon: CircleDot, label: 'Runtime', tone: 'slate' }
}

function inline(value: unknown, maxLength = 180) {
  if (value === undefined || value === null || value === '') return ''
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return text.length > maxLength ? `${text.slice(0, maxLength)}…` : text
}

function eventSummary(event: RuntimeEvent) {
  const p = event.payload
  if (event.type === 'intent.classification.started') return `Taxonomy ${p.taxonomy_version ?? 'active'} · decision ${p.decision_id ?? ''}`
  if (event.type === 'intent.classification.completed') return `${p.primary_intent ?? 'unknown'} · ${Math.round(Number(p.confidence ?? 0) * 100)}% confidence · ${p.summary ?? ''}`
  if (event.type === 'routing.workspace_required') return `${p.resolved ? 'Workspace supplied' : 'Workspace required'}${p.repository_id ? ` · ${p.repository_id}` : ''}`
  if (event.type === 'routing.agent.selected') return `${p.agent_name ?? p.deployment_id ?? 'Agent selected'} · ${p.reason ?? 'policy match'}`
  if (event.type === 'routing.fallback') return `${p.agent_name ?? p.deployment_id ?? 'Fallback Agent'} · ${p.reason ?? 'fallback policy'}`
  if (event.type === 'routing.user_overridden') return `${p.agent_name ?? p.deployment_id ?? 'Agent selected by user'} · manual override`
  if (event.type === 'graph.started') return `${p.graph_name ?? 'Execution graph'} · entry ${p.entry_node ?? 'main'}`
  if (event.type === 'graph.completed') return `${p.graph_name ?? 'Execution graph'} · ${p.status ?? 'completed'}`
  if (event.type === 'graph.failed' || event.type === 'graph.cancelled') return inline(p.message ?? p.reason ?? p.status)
  if (event.type === 'graph.paused') return `${p.node_id ?? 'Graph'} · ${p.reason ?? p.status ?? 'paused'}`
  if (event.type === 'graph.resumed') return `${p.node_id ?? 'Graph'} · checkpoint ${p.checkpoint_id ?? 'restored'}`
  if (event.type === 'graph.node.started') return `${p.node_name ?? p.node_id ?? 'Node'} started`
  if (event.type === 'graph.node.completed') return `${p.node_id ?? 'Node'} · ${p.status ?? 'completed'}${p.result_count !== undefined ? ` · ${p.result_count} results` : ''}`
  if (event.type === 'graph.subgraph.started') return `${p.graph_name ?? 'Subgraph'} · child of ${p.parent_graph_id ?? 'main'}`
  if (event.type === 'graph.subgraph.completed') return `${p.graph_name ?? 'Subgraph'} · ${p.status ?? 'completed'}`
  if (event.type === 'model.started') return `${p.model ?? 'Model'} · ${p.route ?? p.provider ?? 'configured route'}${p.streaming ? ' · streaming' : ''}`
  if (event.type === 'model.reasoning.started') return `${p.reasoning_kind ?? 'reasoning'} · ${p.source ?? p.api_style ?? 'provider stream'}`
  if (event.type === 'model.reasoning.delta') return inline(p.delta)
  if (event.type === 'model.reasoning.completed') return `${p.characters ?? String(p.reasoning ?? '').length} characters${p.reasoning_tokens ? ` · ${p.reasoning_tokens} reasoning tokens` : ''}`
  if (event.type === 'model.delta') return inline(p.delta)
  if (event.type === 'model.completed') return inline(p.output)
  if (event.type === 'tool.requested') return `${p.tool_name ?? 'Tool'} · risk ${p.risk_level ?? 'unknown'}${p.arguments ? ` · ${inline(p.arguments, 120)}` : ''}`
  if (event.type === 'tool.started') return `${p.tool_name ?? 'Tool'} started${p.arguments ? ` · ${inline(p.arguments, 120)}` : ''}`
  if (event.type === 'tool.completed') return `${p.tool_name ?? 'Tool'} completed${p.status ? ` · ${p.status}` : ''}${p.result_count !== undefined ? ` · ${p.result_count} results` : ''}`
  if (event.type === 'tool.failed') return `${p.tool_name ?? 'Tool'} · ${p.message ?? p.reason ?? p.code ?? 'failed'}`
  if (event.type === 'tool.approval_required') return inline(p.policy_reason)
  if (event.type === 'interrupt.created') return `Approval ${p.interrupt_id ?? ''} · checkpoint ${p.checkpoint_id ?? ''}`
  if (event.type === 'interrupt.resolved') return `${p.decision ?? 'Decision recorded'} · ${p.actor ?? 'reviewer'}`
  if (event.type === 'run.waiting_for_input') return inline(p.message ?? p.reason)
  if (event.type === 'run.input_received') return `Input received from ${p.actor ?? 'reviewer'} · attempt ${p.attempt ?? ''}`
  if (event.type.startsWith('subagent.')) return inline(p.task ?? p.summary ?? p.result ?? p.status)
  if (event.type === 'artifact.created') return `${p.name ?? 'Artifact'}${p.media_type ? ` · ${p.media_type}` : ''}`
  if (event.type === 'todo.updated') return `${p.items?.filter((item: any) => item.status === 'completed').length ?? 0}/${p.items?.length ?? 0} steps complete`
  if (event.type === 'usage.updated') return `${Number(p.input_tokens ?? 0) + Number(p.output_tokens ?? 0)} tokens · $${Number(p.cost ?? 0).toFixed(4)}`
  if (event.type === 'skill.loaded') return `${p.slug ?? 'Skill'} · ${p.version ?? 'loaded'}`
  if (event.type === 'run.preparing') return `Worker ${p.worker_id ?? 'assigned'}`
  if (event.type === 'run.completed') return inline(p.output)
  return inline(p.message ?? p.reason ?? p.status ?? p.queue ?? p)
}

export function EventTimeline({ events, compact = false }: { events: RuntimeEvent[]; compact?: boolean }) {
  return <div className={`event-timeline ${compact ? 'compact' : ''}`} role="log" aria-live="polite" aria-relevant="additions">
    {events.map((event) => {
      const meta = eventMeta(event)
      const Icon = meta.icon
      const summary = eventSummary(event)
      const body = <>
        <div className="event-node-wrap"><div className={`event-node tone-${meta.tone}`}><Icon size={14} /></div><span>{meta.label}</span></div>
        <div className="event-copy"><div className="event-heading"><strong>{event.type}</strong><time title={new Date(event.occurred_at).toLocaleString()} dateTime={event.occurred_at}>{formatRelative(event.occurred_at)}</time></div>{summary && <p>{summary}</p>}{event.execution_path?.length > 1 && <span className="event-path"><GitBranch size={12} />{event.execution_path.join(' / ')}</span>}</div>
        <span className="event-sequence">#{event.sequence}</span>
      </>
      if (compact) return <div className="event-row" key={event.event_id}>{body}</div>
      const important = meta.tone === 'rose' || event.type === 'tool.approval_required'
      return <details className={`event-row event-details ${meta.tone === 'rose' ? 'error-event' : ''}`} key={event.event_id} open={important}>
        <summary>{body}</summary>
        <div className="event-payload"><div><span>EVENT ID</span><code>{event.event_id}</code><button aria-label={`Copy event ${event.sequence} JSON`} onClick={() => navigator.clipboard.writeText(JSON.stringify(event, null, 2))}><Copy size={14} /> Copy JSON</button></div><pre>{JSON.stringify(event.payload, null, 2)}</pre></div>
      </details>
    })}
  </div>
}
