import { useEffect, useRef, useState } from 'react'
import { ErrorBanner, LoadingBlock, StatusPill } from './UI'
import { useAuth } from '../context/AuthContext'
import { usePlatform } from '../context/PlatformContext'
import { api } from '../lib/api'
import type { CursorPage, ProductionRoutingProfile, RoutingChangeDraft, RoutingChangeRequest, RoutingDeploymentSummary } from '../types'

const intents = ['general', 'coding', 'knowledge', 'release'] as const
const emptyPage = <T,>(): CursorPage<T> => ({ items: [], has_more: false, next_cursor: null })

export function ProductionRoutingPanel() {
  const { user } = useAuth()
  const { context } = usePlatform()
  const [profile, setProfile] = useState<ProductionRoutingProfile | null>(null)
  const [draft, setDraft] = useState<RoutingChangeDraft | null>(null)
  const [deployments, setDeployments] = useState<RoutingDeploymentSummary[]>([])
  const [history, setHistory] = useState(emptyPage<ProductionRoutingProfile>)
  const [page, setPage] = useState(emptyPage<RoutingChangeRequest>)
  const [selected, setSelected] = useState<RoutingChangeRequest | null>(null)
  const [rights, setRights] = useState({ request: false, approve: false })
  const [reason, setReason] = useState('')
  const [decisionReason, setDecisionReason] = useState('')
  const [reviewConfirmed, setReviewConfirmed] = useState(false)
  const [rollback, setRollback] = useState('')
  const [busy, setBusy] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const submission = useRef<{ fingerprint: string; key: string } | null>(null)

  async function fetchState() {
    const [grants, requests] = await Promise.all([api.releaseGrants(), api.routingChanges()])
    const grant = grants.items.find(item => item.user_id === user?.id && item.environment === 'production')
    const permissions = context?.user.permissions ?? []
    const rights = { request: !!grant?.can_deploy && permissions.includes('routing.request'),
      approve: !!grant?.can_approve && permissions.includes('routing.approve') }
    const canRead = !!(grant?.can_deploy || grant?.can_approve || user?.is_super_admin || user?.roles.includes('tenant_admin'))
    const [routing, revisions] = canRead ? await Promise.all([api.productionRouting(), api.routingHistory()]) : [null, emptyPage<ProductionRoutingProfile>()]
    return { requests, rights, routing, revisions }
  }
  function applyState(data: Awaited<ReturnType<typeof fetchState>>) {
    setRights(data.rights); setPage(data.requests); setHistory(data.revisions)
    setProfile(data.routing?.profile ?? null); setDeployments(data.routing?.deployments ?? [])
    setDraft(data.routing ? { mode: data.routing.profile.mode, ...data.routing.profile.config } : null)
    setRollback(''); setReviewConfirmed(false)
  }
  useEffect(() => {
    let active = true
    setLoaded(false); setSelected(null); submission.current = null; setError(''); setNotice('')
    fetchState().then(data => { if (active) applyState(data) })
      .catch(e => { if (active) setError((e as Error).message) })
      .finally(() => { if (active) setLoaded(true) })
    return () => { active = false }
  }, [user?.id, context?.tenant.id, context?.project.id])

  async function refresh(requestId = selected?.id) {
    applyState(await fetchState())
    if (requestId) {
      try { setSelected(await api.routingChange(requestId)) }
      catch (e) { setSelected(null); throw e }
    }
  }
  async function execute(name: string, action: () => Promise<void>) {
    if (busy) return
    setBusy(name); setError(''); setNotice('')
    try { await action() } catch (e) { setError((e as Error).message) }
    finally { setBusy('') }
  }
  async function submit() {
    if (!profile || !draft || !rights.request) return
    const body = { expected_router_revision_id: profile.id, reason: reason.trim(),
      action: (rollback ? 'rollback' : 'update') as 'rollback' | 'update',
      ...(rollback ? { rollback_revision_id: rollback } : { profile: draft }) }
    const fingerprint = JSON.stringify(body)
    if (submission.current?.fingerprint !== fingerprint) submission.current = { fingerprint, key: crypto.randomUUID() }
    const result = await api.createRoutingChange(body, submission.current.key)
    setSelected(result); setDecisionReason(''); setReason(''); submission.current = null
    setNotice('Submitted for independent review. Production routing has not changed.')
    await refresh(result.id)
  }
  async function decide(action: 'approve' | 'reject' | 'cancel') {
    if (!selected) return
    if (action === 'approve' && !reviewConfirmed) return
    try {
      const result = await api.decideRoutingChange(selected, action, decisionReason.trim())
      setSelected(result); setDecisionReason('')
      setNotice(action === 'approve' ? 'Approved routing revision is now active for new routing decisions.' : `Request ${result.status.toLowerCase()}.`)
    } catch (e) {
      try { setSelected(await api.routingChange(selected.id)) } catch { setSelected(null) }
      throw e
    } finally { await refresh() }
  }
  const pending = selected?.status === 'PENDING'
  const expired = !!selected && (selected.expired || Date.parse(selected.expires_at) <= Date.now())
  const independent = selected?.requested_by !== user?.id

  return <section className="panel production-routing-panel">
    <div className="panel-heading"><div><h3>Production routing reviews</h3><p>Explicit production grants and independent approval are required, including rollback. Conversation access stays in your current environment.</p></div>
      <button className="button ghost" disabled={!!busy || !loaded} onClick={() => void execute('reload', refresh)}>Refresh</button></div>
    {!loaded ? <LoadingBlock label="Loading production routing reviews…" /> : <>
      {error && <ErrorBanner message={error} />}
      {notice && <div className="success-banner" role="status">{notice}</div>}
      {profile && <p>Current revision {profile.revision_number} · {profile.id} · {profile.approval_state === 'APPROVED' ? 'Approved' : 'Legacy configuration: automatic production routing is blocked until reviewed.'}</p>}
      {!rights.request && <p className="policy-callout">You do not have production routing request authority. An administrator must grant production deployment authority; a project role alone is insufficient.</p>}
      {rights.request && profile && draft && <fieldset disabled={!!busy}>
        <legend>Request a production routing change</legend>
        <label>Action<select aria-label="Routing rollback revision" value={rollback} onChange={e => setRollback(e.target.value)}>
          <option value="">Update configuration</option>
          {history.items.filter(item => item.approval_state === 'APPROVED' && item.id !== profile.id).map(item =>
            <option key={item.id} value={item.id}>Rollback to revision {item.revision_number} · {item.id}</option>)}
        </select></label>
        {history.next_cursor && <button className="button ghost" onClick={() => void execute('history', async () => {
          const next = await api.routingHistory(history.next_cursor!); setHistory({ ...next, items: [...history.items, ...next.items] })
        })}>Load older revisions</button>}
        {rollback ? <p>Rollback creates a new reviewed revision. Old targets must still be active and pass current production checks; restore a retired deployment through the release workflow first.</p> : <div className="production-routing-form">
          <label>Mode<select aria-label="Production routing mode" value={draft.mode} onChange={e => setDraft({ ...draft, mode: e.target.value as RoutingChangeDraft['mode'] })}>
            <option value="active">Active</option><option value="shadow">Shadow</option><option value="disabled">Disabled</option>
          </select></label>
          {intents.map(intent => <label key={intent}>{intent} target<select aria-label={`Production ${intent} target`} value={draft.target_deployments[intent] ?? ''}
            onChange={e => setDraft({ ...draft, target_deployments: { ...draft.target_deployments, [intent]: e.target.value || null } })}>
            <option value="">{intent === 'general' ? 'None: no default route' : 'Use explicitly configured general target'}</option>
            {deployments.filter(item => intent === 'coding' ? item.coding_enabled : intent === 'knowledge' ? item.knowledge_enabled : !item.coding_enabled)
              .map(item => <option key={item.id} value={item.id}>{item.agent_name} · {item.id}</option>)}
          </select></label>)}
          <label>Automatic threshold<input aria-label="Production automatic threshold" type="number" min="0" max="1" step="0.05" value={draft.auto_route_threshold} onChange={e => setDraft({ ...draft, auto_route_threshold: Number(e.target.value) })} /></label>
          <label>Confirmation threshold<input aria-label="Production confirmation threshold" type="number" min="0" max="1" step="0.05" value={draft.confirmation_threshold} onChange={e => setDraft({ ...draft, confirmation_threshold: Number(e.target.value) })} /></label>
          <label>Decision validity (seconds)<input aria-label="Production decision validity" type="number" min="60" max="3600" value={draft.decision_ttl_seconds} onChange={e => setDraft({ ...draft, decision_ttl_seconds: Number(e.target.value) })} /></label>
        </div>}
        <label>Request reason<textarea aria-label="Routing request reason" value={reason} maxLength={1000} onChange={e => setReason(e.target.value)} /></label>
        <button className="button primary" disabled={reason.trim().length < 5 || (!rollback && (
          !Number.isFinite(draft.auto_route_threshold) || !Number.isFinite(draft.confirmation_threshold) ||
          draft.auto_route_threshold < 0 || draft.auto_route_threshold > 1 || draft.confirmation_threshold < 0 ||
          draft.confirmation_threshold > draft.auto_route_threshold || !Number.isInteger(draft.decision_ttl_seconds) ||
          draft.decision_ttl_seconds < 60 || draft.decision_ttl_seconds > 3600))}
          onClick={() => void execute('submit', submit)}>Submit routing review</button>
      </fieldset>}
      <div className="production-routing-requests">
        <h4>Change requests</h4>
        {!page.items.length && <p>No visible routing change requests.</p>}
        {page.items.map(item => <button className="button ghost" key={item.id} disabled={!!busy} onClick={() => void execute('select', async () => {
          setSelected(await api.routingChange(item.id)); setDecisionReason(''); setReviewConfirmed(false)
        })}>{item.action} · {item.id} <StatusPill status={item.status} />{item.expired ? ' · Expired' : ''}</button>)}
        {page.next_cursor && <button className="button ghost" disabled={!!busy} onClick={() => void execute('more', async () => {
          const next = await api.routingChanges(page.next_cursor!); setPage({ ...next, items: [...page.items, ...next.items] })
        })}>Load more requests</button>}
      </div>
      {selected && <div className="production-routing-review">
        <h4>Review {selected.id}</h4><p>{selected.reason}</p>
        <p>Requested by {selected.requested_by} · Expires {new Date(selected.expires_at).toLocaleString()} · <StatusPill status={selected.status} /></p>
        <div className="production-routing-diff"><div><h5>Before · revision {selected.before.revision_number}</h5><pre>{JSON.stringify({ mode: selected.before.mode, ...selected.before.config }, null, 2)}</pre></div>
          <div><h5>After · {selected.action}</h5><pre>{JSON.stringify(selected.after, null, 2)}</pre></div></div>
        <details><summary>Locked deployment and evaluation evidence</summary><pre>{JSON.stringify(selected.targets, null, 2)}</pre><small>Review hash: {selected.snapshot_hash}</small></details>
        {selected.decision_reason && <p>Decision by {selected.decided_by}: {selected.decision_reason}</p>}
        {pending && <>
          {expired && <p className="policy-callout">This request expired. Cancel it or submit a fresh review.</p>}
          {!independent && <p>You cannot approve your own request.</p>}
          <label>Decision reason<textarea aria-label="Routing decision reason" value={decisionReason} maxLength={1000} onChange={e => setDecisionReason(e.target.value)} disabled={!!busy} /></label>
          {independent && rights.approve && <><label className="routing-review-confirmation"><input type="checkbox" checked={reviewConfirmed} disabled={!!busy || expired}
            onChange={e => setReviewConfirmed(e.target.checked)} />I reviewed the exact routing changes and approve applying them to production.</label>
            <button className="button primary" disabled={!!busy || expired || !reviewConfirmed || decisionReason.trim().length < 5} onClick={() => void execute('approve', () => decide('approve'))}>Approve routing change</button>
            <button className="button ghost" disabled={!!busy || expired || decisionReason.trim().length < 5} onClick={() => void execute('reject', () => decide('reject'))}>Reject</button></>}
          {!independent && <button className="button ghost" disabled={!!busy || decisionReason.trim().length < 5} onClick={() => void execute('cancel', () => decide('cancel'))}>Cancel request</button>}
        </>}
      </div>}
    </>}
  </section>
}
