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
  ShieldAlert,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { EmptyState, ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative, shortId } from '../components/UI'
import { api } from '../lib/api'
import type { Interrupt } from '../types'
import { usePlatform } from '../context/PlatformContext'

export function ApprovalsPage() {
  const [items, setItems] = useState<Interrupt[]>([])
  const [selected, setSelected] = useState<Interrupt | null>(null)
  const [history, setHistory] = useState<Interrupt[]>([])
  const [message, setMessage] = useState('')
  const [editing, setEditing] = useState(false)
  const [argumentsText, setArgumentsText] = useState('')
  const [outcome, setOutcome] = useState<{ type: string; runId: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [loaded, setLoaded] = useState(false)
  const { refresh: refreshPlatform } = usePlatform()

  const load = async () => {
    try {
      const result = await api.interrupts()
      const pending = result.items.filter((item) => item.status === 'PENDING').sort((a, b) => a.created_at.localeCompare(b.created_at))
      setItems(pending); setHistory(result.items.filter((item) => item.status !== 'PENDING'))
      setSelected((current) => current && pending.some((item) => item.id === current.id) ? current : pending[0] ?? null)
    } catch (nextError) { setError((nextError as Error).message) } finally { setLoaded(true) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => { if (selected) setArgumentsText(JSON.stringify(selected.actions[0]?.arguments ?? {}, null, 2)) }, [selected])

  const expiringSoon = useMemo(() => items.filter((item) => {
    const remaining = new Date(item.expires_at).getTime() - Date.now()
    return remaining > 0 && remaining < 60 * 60 * 1000
  }).length, [items])
  const decide = async (type: 'approve' | 'edit' | 'reject' | 'respond') => {
    if (!selected) return
    let editedArguments: Record<string, unknown> | undefined
    if (type === 'edit') {
      try { editedArguments = JSON.parse(argumentsText) } catch { setError('Tool arguments must be valid JSON before requesting an edited execution.'); return }
    }
    if (type === 'respond' && !message.trim()) { setError('Add a reviewer note explaining what needs to change.'); return }
    if (type === 'approve' && !window.confirm(`Approve ${selected.actions[0]?.tool_name} and resume this run?`)) return
    if (type === 'reject' && !window.confirm('Reject this action and cancel the run?')) return
    setBusy(type); setError('')
    try {
      const runId = selected.run_id; await api.decide(selected, type, message || undefined, editedArguments)
      setOutcome({ type, runId }); setSelected(null); setMessage(''); setEditing(false); await Promise.all([load(), refreshPlatform()])
    } catch (nextError) { setError((nextError as Error).message) } finally { setBusy('') }
  }

  if (!loaded) return <LoadingBlock />

  return <div className="page-stack approvals-page">
    <PageHeader eyebrow="HUMAN IN THE LOOP" title="Review policy-gated actions" description="Inspect the target, arguments, policy reason, checkpoint, and original request before resolving an interrupt." />
    {error && <ErrorBanner message={error} />}
    {outcome && <div className="success-banner approval-outcome"><Check size={17} /><div><strong>{outcome.type === 'approve' ? 'Approved and queued for resume.' : outcome.type === 'edit' ? 'Edited arguments submitted and queued.' : outcome.type === 'reject' ? 'Rejected and run cancelled.' : 'Changes requested; run is waiting for input.'}</strong><span>The decision is recorded in the immutable audit trail.</span></div><Link className="button secondary" to={`/advanced/runs/${outcome.runId}`}>View run <ArrowRight size={14} /></Link></div>}
    <div className="approval-summary"><div><div className="summary-icon tone-amber"><Clock3 size={19} /></div><span>Waiting review</span><strong>{items.length}</strong></div><div><div className="summary-icon tone-green"><CheckCircle2 size={19} /></div><span>Resolved</span><strong>{history.length}</strong></div><div><div className="summary-icon tone-rose"><AlertTriangle size={19} /></div><span>Expiring soon</span><strong>{expiringSoon}</strong></div></div>

    {items.length === 0 ? <div className="panel"><EmptyState icon={FileCheck2} title="Approval queue is clear" description="Policy-gated actions from active runs will appear here with their checkpoint and execution context." action={<Link className="button primary" to="/advanced/runs">View runs</Link>} /></div> : <div className="approval-layout">
      <section className="panel approval-queue"><div className="panel-heading"><div><h3>Pending requests</h3><p>Oldest requests appear first</p></div><span className="count-badge warning">{items.length}</span></div><div className="approval-list">{items.map((item) => <button key={item.id} className={`approval-list-item ${selected?.id === item.id ? 'selected' : ''}`} onClick={() => setSelected(item)}><div className="risk-icon"><ShieldAlert size={18} /></div><div><div><strong>{item.agent_name}</strong><StatusPill status={item.actions[0]?.risk_level ?? 'risk'} /></div><p>{item.actions[0]?.tool_name}</p><span>{shortId(item.run_id)} · waiting {formatRelative(item.created_at).replace(' ago', '')}</span></div><ArrowRight size={16} /></button>)}</div></section>
      {selected && <section className="panel approval-detail"><div className="approval-detail-head"><div><span className="page-eyebrow">APPROVAL REQUEST</span><h3>{selected.actions[0].tool_name}</h3><p>{selected.policy_reason}</p></div><StatusPill status="PENDING" /></div><div className="approval-context"><div><span>Agent</span><strong>{selected.agent_name}</strong></div><div><span>Run</span><Link to={`/advanced/runs/${selected.run_id}`}><code>{shortId(selected.run_id)}</code></Link></div><div><span>Checkpoint</span><code>{shortId(selected.checkpoint_id)}</code></div><div><span>Expires</span><strong title={new Date(selected.expires_at).toLocaleString()}>{formatRelative(selected.expires_at)}</strong></div></div><div className="risk-banner"><AlertTriangle size={19} /><div><strong>{selected.actions[0]?.risk_level ?? 'High'}-risk operation</strong><p>Credentials remain outside the checkpoint and are resolved only after a permitted decision.</p></div></div><div className="argument-block"><div><span>TOOL ARGUMENTS</span><button onClick={() => setEditing((value) => !value)}><PencilLine size={13} />{editing ? 'Cancel edit' : 'Edit arguments'}</button></div>{editing ? <textarea className="argument-editor" rows={9} value={argumentsText} onChange={(event) => setArgumentsText(event.target.value)} spellCheck={false} /> : <pre>{argumentsText}</pre>}</div><div className="request-context"><span>ORIGINAL REQUEST</span><p>“{selected.run_input}”</p></div><label className="review-message">Reviewer note (optional)<textarea rows={3} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="Add context for the audit trail…" /></label><div className="decision-actions"><button className="button danger" disabled={!!busy} onClick={() => void decide('reject')}>{busy === 'reject' ? <LoaderCircle className="spin" size={15} /> : <XCircle size={15} />} Reject</button><button className="button secondary" disabled={!!busy} onClick={() => void decide('respond')}><MessageSquare size={15} /> Request changes</button>{editing ? <button className="button approve" disabled={!!busy} onClick={() => void decide('edit')}><PencilLine size={15} /> Submit edited action</button> : <button className="button approve" disabled={!!busy} onClick={() => void decide('approve')}>{busy === 'approve' ? <LoaderCircle className="spin" size={15} /> : <ShieldCheck size={15} />} Approve & resume</button>}</div></section>}
    </div>}

    {history.length > 0 && <section className="panel approval-history"><div className="panel-heading"><div><h3>Decision history</h3><p>Immutable audit trail for resolved interrupts</p></div></div><div className="table-wrap"><table className="data-table"><thead><tr><th>Interrupt</th><th>Agent</th><th>Tool</th><th>Decision</th><th>Resolved</th></tr></thead><tbody>{history.map((item) => <tr key={item.id}><td><code>{shortId(item.id)}</code></td><td><strong className="table-strong">{item.agent_name}</strong></td><td>{item.actions[0]?.tool_name}</td><td><StatusPill status={String((item.decision as any)?.decisions?.[0]?.type ?? 'RESOLVED')} /></td><td><time className="table-time" dateTime={item.updated_at ?? item.created_at}>{new Date(item.updated_at ?? item.created_at).toLocaleString()}</time></td></tr>)}</tbody></table></div></section>}
  </div>
}
