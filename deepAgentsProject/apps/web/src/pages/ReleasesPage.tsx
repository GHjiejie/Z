import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { RefreshCw, ShieldCheck, X } from 'lucide-react'
import { api, ApiError } from '../lib/api'
import type { Agent, CursorPage, EnvironmentGrant, ReleaseChannel, ReleaseRequest } from '../types'
import { EmptyState, ErrorBanner, LoadingBlock, PageHeader, StatusPill } from '../components/UI'
import { useAuth } from '../context/AuthContext'
import { usePlatform } from '../context/PlatformContext'

export function ReleasesPage() {
  const { user } = useAuth()
  const { context, refresh } = usePlatform()
  const [params] = useSearchParams()
  const [agents, setAgents] = useState<Agent[]>([])
  const [agentId, setAgentId] = useState(params.get('agent') ?? '')
  const [agent, setAgent] = useState<Agent | null>(null)
  const [revisionId, setRevisionId] = useState(params.get('revision') ?? '')
  const [channel, setChannel] = useState<ReleaseChannel | null>(null)
  const [grants, setGrants] = useState<EnvironmentGrant[]>([])
  const [page, setPage] = useState<CursorPage<ReleaseRequest>>({ items: [], has_more: false, next_cursor: null })
  const [selected, setSelected] = useState<ReleaseRequest | null>(null)
  const [reason, setReason] = useState('')
  const [note, setNote] = useState('')
  const [rollbackId, setRollbackId] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [candidateReload, setCandidateReload] = useState(0)
  const submission = useRef<{ fingerprint: string; key: string } | null>(null)
  const ownGrant = grants.find(item => item.user_id === user?.id && item.environment === 'production')
  const canRequest = !!ownGrant?.can_deploy && !!context?.user.permissions.includes('deployment.manage')
  const canApprove = !!ownGrant?.can_approve && !!context?.user.permissions.includes('release.approve')

  const load = async () => {
    const [list, authority, candidates] = await Promise.all([api.releaseRequests(), api.releaseGrants(), api.agents()])
    setPage(list); setGrants(authority.items); setAgents(candidates.items)
  }
  useEffect(() => { let active = true
    Promise.all([api.releaseRequests(), api.releaseGrants(), api.agents()]).then(([list, authority, candidates]) => {
      if (active) { setPage(list); setGrants(authority.items); setAgents(candidates.items) }
    }).catch(e => { if (active) setError((e as Error).message) }).finally(() => { if (active) setLoaded(true) })
    return () => { active = false }
  }, [])

  useEffect(() => { let active = true
    setAgent(null); setChannel(null)
    if (agentId && canRequest) {
      Promise.all([api.agent(agentId), api.releaseChannel(agentId)]).then(([detail, current]) => {
        if (active) {
          setAgent(detail); setChannel(current)
          setRevisionId(value => detail.revisions?.some(item => item.id === value) ? value : detail.revisions?.[0]?.id ?? '')
        }
      }).catch(e => { if (active) setError((e as Error).message) })
    }
    return () => { active = false }
  }, [agentId, canRequest, candidateReload])

  const submit = async () => {
    if (!channel || !revisionId || !canRequest || busy) return
    const body = { agent_revision_id: revisionId, expected_channel_version: channel.version, reason: reason.trim(),
      action: (rollbackId ? 'rollback' : 'promote') as 'rollback' | 'promote',
      ...(rollbackId ? { rollback_deployment_id: rollbackId } : {}) }
    const fingerprint = JSON.stringify(body)
    if (submission.current?.fingerprint !== fingerprint) submission.current = { fingerprint, key: crypto.randomUUID() }
    setBusy('submit'); setError(''); setNotice('')
    try {
      const result = await api.createRelease(body, submission.current.key)
      setSelected(result); setNote(''); setReason(''); submission.current = null
      setNotice('Request submitted. Production has not changed; another authorized reviewer must approve it.')
      await load()
    } catch (e) {
      setError((e as Error).message)
      if (e instanceof ApiError && [403, 409].includes(e.status)) setChannel(null)
    } finally { setBusy('') }
  }

  const decide = async (action: 'approve' | 'reject' | 'cancel') => {
    if (!selected || busy || note.trim().length < 5) return
    if (action === 'approve' && !window.confirm('Approve and apply this exact production release, including its listed routing changes?')) return
    setBusy(action); setError(''); setNotice('')
    try {
      const result = action === 'cancel' ? await api.cancelRelease(selected, note.trim()) : await api.decideRelease(selected, action, note.trim())
      setSelected(result)
      setNotice(action === 'approve' ? 'Release applied. New tasks use the approved deployment; existing tasks may finish.' : `Request ${result.status.toLowerCase()}.`)
      await load(); await refresh(); setCandidateReload(value => value + 1)
    } catch (e) {
      setError((e as Error).message)
      try { setSelected(await api.releaseRequest(selected.id)) } catch { setSelected(null) }
    } finally { setBusy('') }
  }

  const reload = async () => {
    setBusy('reload'); setError('')
    try { await load(); setCandidateReload(value => value + 1) } catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }

  const more = async () => {
    if (!page.next_cursor || busy) return
    setBusy('more'); setError('')
    try { const next = await api.releaseRequests({ cursor: page.next_cursor }); setPage({ ...next, items: [...page.items, ...next.items] }) }
    catch (e) { setError((e as Error).message) } finally { setBusy('') }
  }
  const expired = selected && (selected.expired || Date.parse(selected.expires_at) <= Date.now())
  const ownRequest = selected?.requested_by === user?.id
  const pending = selected?.status === 'PENDING'

  return <div className="page-stack release-governance">
    <PageHeader eyebrow="PRODUCTION GOVERNANCE" title="Production releases" description="Request, independently review, and apply an immutable revision. Publishing an Agent does not authorize a production release." actions={<button className="button secondary" disabled={!!busy} onClick={() => void reload()}><RefreshCw size={16} /> Refresh</button>} />
    {error && <ErrorBanner message={error} />}
    {notice && <div className="success-banner" role="status">{notice}</div>}
    {!loaded ? <LoadingBlock /> : <>
      <section className="panel release-request-form" aria-labelledby="release-request-heading">
        <h3 id="release-request-heading">Request a production change</h3>
        <p>Requires an approved model profile, current passing evaluation, and an explicit production grant. Rollbacks require the same independent review.</p>
        {!canRequest ? <p className="policy-callout">You do not currently have production request authority. Ask a tenant administrator to grant it; project membership alone is not sufficient.</p> : <>
          <div className="release-fields"><label>Agent<select value={agentId} disabled={!!busy} onChange={event => { setAgentId(event.target.value); setRollbackId(''); setRevisionId(''); setChannel(null); setError('') }}><option value="">Select an Agent</option>{agents.map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label>
          <label>Immutable revision<select value={revisionId} disabled={!!busy || !agent} onChange={event => { setRevisionId(event.target.value); setRollbackId('') }}>{!agent?.revisions?.length && <option value="">No revision selected</option>}{agent?.revisions?.map(item => <option value={item.id} key={item.id}>Revision {item.revision_number} · {item.id}</option>)}</select></label>
          <label>Change type<select value={rollbackId} disabled={!!busy || !agent} onChange={event => { setRollbackId(event.target.value); const target = agent?.deployments?.find(item => item.id === event.target.value); if (target) setRevisionId(target.agent_revision_id) }}><option value="">Promote selected revision</option>{agent?.deployments?.filter(item => item.environment === 'production' && item.id !== channel?.active_deployment_id).map(item => <option value={item.id} key={item.id}>Rollback to {item.name} · {item.id}</option>)}</select></label></div>
          <p>{channel ? `Reviewed channel version: ${channel.version} · Active deployment: ${channel.active_deployment_id ?? 'None'}` : 'Select an Agent or refresh to load its current production channel.'}</p>
          <label>Change reason<textarea value={reason} maxLength={1000} disabled={!!busy} onChange={event => setReason(event.target.value)} placeholder="Explain the change, its risk, and the rollback plan." /></label>
          <button className="button primary" disabled={!!busy || !channel || !revisionId || reason.trim().length < 5} onClick={() => void submit()}>{busy === 'submit' ? 'Submitting…' : 'Submit for independent review'}</button>
        </>}
      </section>
      <section className="panel release-history" aria-labelledby="release-history-heading"><h3 id="release-history-heading">Requests and history</h3>
        {!page.items.length ? <EmptyState icon={ShieldCheck} title="No visible release requests" description="Requests appear here when you create them or receive production review authority." /> : <div className="release-request-list">{page.items.map(item => <button key={item.id} className="release-request-row" disabled={!!busy} onClick={() => { setSelected(item); setNote(''); setError('') }}><span><strong>{agents.find(agent => agent.id === item.agent_id)?.name ?? item.agent_id}</strong><small>{item.action} · {item.agent_revision_id}</small></span><StatusPill status={item.expired ? 'EXPIRED' : item.status} /><span>{new Date(item.created_at).toLocaleString()}</span></button>)}</div>}
        {page.has_more && <button className="button secondary" disabled={!!busy} onClick={() => void more()}>Load more requests</button>}
      </section>
    </>}
    {selected && <div className="modal-backdrop"><section className="modal release-review-modal" role="dialog" aria-modal="true" aria-labelledby="release-review-heading">
      {error && <ErrorBanner message={error} />}
      <div className="modal-heading"><div><h3 id="release-review-heading">Review production {selected.action}</h3><StatusPill status={expired ? 'EXPIRED' : selected.status} /></div><button className="icon-button" aria-label="Close release review" disabled={!!busy} onClick={() => setSelected(null)}><X size={18} /></button></div>
      <dl className="release-evidence"><dt>Revision</dt><dd>{selected.agent_revision_id}</dd><dt>Plan hash</dt><dd>{selected.plan_hash}</dd><dt>Evaluation</dt><dd>{selected.evaluation_id}</dd><dt>Requested by</dt><dd>{selected.requested_by}</dd><dt>Channel version</dt><dd>{selected.expected_channel_version}</dd><dt>Routing changes</dt><dd>{selected.routing.targets.join(', ') || 'No existing routes retargeted'}</dd><dt>Expires</dt><dd>{new Date(selected.expires_at).toLocaleString()}</dd><dt>Reason</dt><dd>{selected.reason}</dd>{selected.deployment_id && <><dt>Applied deployment</dt><dd>{selected.deployment_id}</dd></>}{selected.decision_reason && <><dt>Review note</dt><dd>{selected.decision_reason}</dd></>}</dl>
      {pending && <>{ownRequest && <p className="policy-callout">You requested this release. Another authorized person must approve it.</p>}{!ownRequest && !canApprove && <p>You do not have production approval authority.</p>}<label>Review or cancellation note<textarea value={note} maxLength={1000} disabled={!!busy} onChange={event => setNote(event.target.value)} /></label><div className="modal-actions">{ownRequest ? <button className="button secondary" disabled={!!busy || note.trim().length < 5} onClick={() => void decide('cancel')}>Cancel request</button> : canApprove && <><button className="button secondary" disabled={!!busy || !!expired || note.trim().length < 5} onClick={() => void decide('reject')}>Reject</button><button className="button primary" disabled={!!busy || !!expired || note.trim().length < 5} onClick={() => void decide('approve')}>Approve and apply</button></>}</div></>}
    </section></div>}
  </div>
}
