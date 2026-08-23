import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Clock3,
  FileCheck2,
  LoaderCircle,
  MessageSquare,
  PencilLine,
  PlayCircle,
  ShieldAlert,
  ShieldCheck,
  X,
  XCircle,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'
import { api } from '../lib/api'
import type { Interrupt } from '../types'

export function ApprovalsPage() {
  const [items, setItems] = useState<Interrupt[]>([])
  const [selected, setSelected] = useState<Interrupt | null>(null)
  const [history, setHistory] = useState<Interrupt[]>([])
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)

  const load = async () => {
    try {
      const result = await api.interrupts()
      setItems(result.items.filter((item) => item.status === 'PENDING'))
      setHistory(result.items.filter((item) => item.status !== 'PENDING'))
      if (!selected) setSelected(result.items.find((item) => item.status === 'PENDING') ?? null)
      setLoaded(true)
    } catch (err) { setError((err as Error).message); setLoaded(true) }
  }
  useEffect(() => { void load() }, [])

  const decide = async (type: 'approve' | 'reject' | 'respond') => {
    if (!selected) return
    setBusy(type); setError('')
    try {
      await api.decide(selected, type, message || undefined)
      setSelected(null); setMessage(''); await load()
    } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }

  if (!loaded) return <LoadingBlock />

  return <div className="page-stack">
    <PageHeader eyebrow="HUMAN IN THE LOOP" title="Review actions at the policy boundary" description="Runs are durably checkpointed before approval. Decisions are versioned, idempotent, and fully auditable." />
    {error && <ErrorBanner message={error} />}
    <div className="approval-summary">
      <div><div className="summary-icon tone-amber"><Clock3 size={19} /></div><span>Waiting review</span><strong>{items.length}</strong></div>
      <div><div className="summary-icon tone-green"><CheckCircle2 size={19} /></div><span>Resolved</span><strong>{history.length}</strong></div>
      <div><div className="summary-icon tone-violet"><ShieldCheck size={19} /></div><span>Policy coverage</span><strong>100%</strong></div>
    </div>

    {items.length === 0 ? <div className="panel"><EmptyState icon={FileCheck2} title="Approval queue is clear" description="Start a Playground run containing “deploy to production” to exercise a high-risk tool interrupt." action={<Link className="button primary" to="/playground"><PlayCircle size={15} /> Open playground</Link>} /></div> : <div className="approval-layout">
      <section className="panel approval-queue"><div className="panel-heading"><div><h3>Pending requests</h3><p>Oldest requests appear first</p></div><span className="count-badge warning">{items.length}</span></div><div className="approval-list">{items.map((item) => <button key={item.id} className={`approval-list-item ${selected?.id === item.id ? 'selected' : ''}`} onClick={() => setSelected(item)}><div className="risk-icon"><ShieldAlert size={18} /></div><div><div><strong>{item.agent_name}</strong><StatusPill status={item.actions[0]?.risk_level ?? 'risk'} /></div><p>{item.actions[0]?.tool_name}</p><span>{shortId(item.run_id)} · {formatRelative(item.created_at)}</span></div><ArrowRight size={16} /></button>)}</div></section>
      {selected && <section className="panel approval-detail"><div className="approval-detail-head"><div><span className="page-eyebrow">APPROVAL REQUEST</span><h3>{selected.actions[0].tool_name}</h3><p>{selected.policy_reason}</p></div><StatusPill status="PENDING" /></div><div className="approval-context"><div><span>Agent</span><strong>{selected.agent_name}</strong></div><div><span>Run</span><code>{shortId(selected.run_id)}</code></div><div><span>Checkpoint</span><code>{shortId(selected.checkpoint_id)}</code></div><div><span>Expires</span><strong>{new Date(selected.expires_at).toLocaleDateString()}</strong></div></div><div className="risk-banner"><AlertTriangle size={19} /><div><strong>High-risk write operation</strong><p>This action targets a production-scoped artifact. Credentials remain outside the checkpoint and will be resolved only after approval.</p></div></div><div className="argument-block"><div><span>TOOL ARGUMENTS</span><button><PencilLine size={13} /> Edit</button></div><pre>{JSON.stringify(selected.actions[0].arguments, null, 2)}</pre></div><div className="request-context"><span>ORIGINAL REQUEST</span><p>“{selected.run_input}”</p></div><label className="review-message">Reviewer note (optional)<textarea rows={3} value={message} onChange={(e) => setMessage(e.target.value)} placeholder="Add context for the audit trail…" /></label><div className="decision-actions"><button className="button danger" disabled={!!busy} onClick={() => void decide('reject')}>{busy === 'reject' ? <LoaderCircle className="spin" size={15} /> : <XCircle size={15} />} Reject</button><button className="button secondary" disabled={!!busy} onClick={() => void decide('respond')}><MessageSquare size={15} /> Request changes</button><button className="button approve" disabled={!!busy} onClick={() => void decide('approve')}>{busy === 'approve' ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />} Approve & resume</button></div></section>}
    </div>}

    {history.length > 0 && <section className="panel approval-history"><div className="panel-heading"><div><h3>Decision history</h3><p>Immutable audit trail for resolved interrupts</p></div></div><div className="table-wrap"><table className="data-table"><thead><tr><th>Interrupt</th><th>Agent</th><th>Tool</th><th>Decision</th><th>Resolved</th></tr></thead><tbody>{history.map((item) => <tr key={item.id}><td><code>{shortId(item.id)}</code></td><td><strong className="table-strong">{item.agent_name}</strong></td><td>{item.actions[0]?.tool_name}</td><td><StatusPill status={String((item.decision as any)?.decisions?.[0]?.type ?? 'RESOLVED')} /></td><td><span className="table-time">{formatRelative(item.created_at)}</span></td></tr>)}</tbody></table></div></section>}
  </div>
}

