import {
  Bot,
  CheckCircle2,
  CircleDot,
  Copy,
  FileText,
  GitBranch,
  Hammer,
  PauseCircle,
  Play,
  Sparkles,
  XCircle,
} from 'lucide-react'
import type { RuntimeEvent } from '../types'
import { formatRelative } from './UI'

function eventMeta(type: string) {
  if (type.startsWith('model.')) return { icon: Sparkles, label: 'Model', tone: 'violet' }
  if (type.startsWith('tool.')) return { icon: Hammer, label: 'Tool', tone: 'amber' }
  if (type.startsWith('subagent.')) return { icon: Bot, label: 'SubAgent', tone: 'cyan' }
  if (type.startsWith('artifact.')) return { icon: FileText, label: 'Artifact', tone: 'green' }
  if (type.startsWith('interrupt.') || type.includes('approval')) return { icon: PauseCircle, label: 'Approval', tone: 'rose' }
  if (type.startsWith('todo.')) return { icon: CheckCircle2, label: 'Plan', tone: 'blue' }
  if (type.includes('failed') || type.includes('cancel')) return { icon: XCircle, label: 'Error', tone: 'rose' }
  if (type.includes('started') || type.includes('resumed')) return { icon: Play, label: 'Runtime', tone: 'green' }
  return { icon: CircleDot, label: 'Runtime', tone: 'slate' }
}

function eventSummary(event: RuntimeEvent) {
  const p = event.payload
  if (event.type === 'model.delta') return p.delta
  if (event.type === 'model.completed') return p.output
  if (event.type === 'tool.requested') return `${p.tool_name} · risk ${p.risk_level}`
  if (event.type === 'tool.completed') return `${p.tool_name} completed`
  if (event.type === 'tool.approval_required') return p.policy_reason
  if (event.type.startsWith('subagent.')) return p.task ?? p.summary ?? p.result
  if (event.type === 'artifact.created') return p.name
  if (event.type === 'todo.updated') return `${p.items?.filter((item: any) => item.status === 'completed').length ?? 0}/${p.items?.length ?? 0} steps complete`
  if (event.type === 'usage.updated') return `${p.input_tokens + p.output_tokens} tokens · $${Number(p.cost).toFixed(4)}`
  if (event.type === 'run.preparing') return `Worker ${p.worker_id}`
  if (event.type === 'run.completed') return p.output
  return p.message ?? p.reason ?? p.status ?? p.queue ?? ''
}

export function eventCategory(event: RuntimeEvent) {
  return eventMeta(event.type).label
}

export function EventTimeline({ events, compact = false }: { events: RuntimeEvent[]; compact?: boolean }) {
  return <div className={`event-timeline ${compact ? 'compact' : ''}`}>
    {events.map((event) => {
      const meta = eventMeta(event.type)
      const Icon = meta.icon
      const summary = eventSummary(event)
      const body = <>
        <div className="event-node-wrap"><div className={`event-node tone-${meta.tone}`}><Icon size={14} /></div><span>{meta.label}</span></div>
        <div className="event-copy"><div className="event-heading"><strong>{event.type}</strong><time title={new Date(event.occurred_at).toLocaleString()} dateTime={event.occurred_at}>{formatRelative(event.occurred_at)}</time></div>{summary && <p>{String(summary)}</p>}{event.execution_path?.length > 1 && <span className="event-path"><GitBranch size={12} />{event.execution_path.join(' / ')}</span>}</div>
        <span className="event-sequence">#{event.sequence}</span>
      </>
      if (compact) return <div className="event-row" key={event.event_id}>{body}</div>
      return <details className={`event-row event-details ${meta.label === 'Error' ? 'error-event' : ''}`} key={event.event_id} open={meta.label === 'Error'}><summary>{body}</summary><div className="event-payload"><div><span>EVENT ID</span><code>{event.event_id}</code><button aria-label={`Copy event ${event.sequence} JSON`} onClick={() => navigator.clipboard.writeText(JSON.stringify(event, null, 2))}><Copy size={14} /> Copy JSON</button></div><pre>{JSON.stringify(event.payload, null, 2)}</pre></div></details>
    })}
  </div>
}
