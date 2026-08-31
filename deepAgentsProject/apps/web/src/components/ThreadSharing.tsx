import { useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ThreadAccess } from '../types'
import { ErrorBanner } from './UI'

export function ThreadSharing({ threadId }: { threadId: string }) {
  const [policy, setPolicy] = useState<ThreadAccess | null>(null)
  const [visibility, setVisibility] = useState<ThreadAccess['visibility']>('private')
  const [members, setMembers] = useState<ThreadAccess['members']>([])
  const [query, setQuery] = useState('')
  const [candidates, setCandidates] = useState<Array<{ id: string; username: string; display_name: string }>>([])
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const load = (next: ThreadAccess) => { setPolicy(next); setVisibility(next.visibility); setMembers(next.members) }
  useEffect(() => {
    let current = true
    setPolicy(null); setError(''); setNotice('')
    void api.threadAccess(threadId).then((next) => { if (current) load(next) }).catch((err: Error) => { if (current) setError(err.message) })
    return () => { current = false }
  }, [threadId])
  useEffect(() => {
    if (!policy?.can_manage || visibility !== 'members') return
    let current = true
    const timer = window.setTimeout(() => {
      void api.sharingCandidates(threadId, query).then((result) => { if (current) setCandidates(result.items) }).catch((err: Error) => { if (current) setError(err.message) })
    }, 250)
    return () => { current = false; window.clearTimeout(timer) }
  }, [threadId, query, visibility, policy?.can_manage])

  const save = async () => {
    if (!policy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const next = await api.updateThreadAccess(threadId, { version: policy.version, visibility, members: visibility === 'members' ? members : [], reason: reason.trim() })
      load(next); setReason(''); setNotice('Sharing updated. Source document permissions still apply.')
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }

  return <section className="panel page-stack" aria-label="Conversation sharing">
    <h4>Conversation sharing</h4>
    {error && <ErrorBanner message={error} />}
    {notice && <p role="status">{notice}</p>}
    {!policy ? <p>Loading sharing settings…</p> : !policy.can_manage ? <p>Shared with you. Only the creator can change access.</p> : <>
      <p>Sharing includes all turns, traces and files in this conversation. A copied link does not grant access.</p>
      {policy.source_restricted && <p>Readers must also have access to every source document used in this conversation.</p>}
      {policy.legacy_access && <p>Historical sharing consent is unknown. This conversation remains private; start a new conversation to collaborate.</p>}
      <label>Who can access this conversation?<select value={visibility} disabled={busy || policy.legacy_access} onChange={(event) => { setVisibility(event.target.value as ThreadAccess['visibility']); setNotice('') }}>
        <option value="private">Only me</option><option value="members">Selected project members</option><option value="project">Project members — read and contribute within their roles</option>
      </select></label>
      {visibility === 'members' && <>
        <label>Find a project member<input value={query} disabled={busy} onChange={(event) => setQuery(event.target.value)} placeholder="Search name or username" /></label>
        <div>{candidates.filter((person) => !members.some((member) => member.user_id === person.id) && person.id !== policy.owner_user_id).map((person) => <button key={person.id} className="button secondary" disabled={busy} onClick={() => setMembers((previous) => [...previous, { user_id: person.id, access: 'read' }])}>Add {person.display_name} ({person.username})</button>)}</div>
        {members.map((member) => <div key={member.user_id}>
          <span>{candidates.find((person) => person.id === member.user_id)?.display_name ?? member.user_id}</span>
          <select aria-label={`Access for ${member.user_id}`} value={member.access} disabled={busy} onChange={(event) => setMembers((previous) => previous.map((item) => item.user_id === member.user_id ? { ...item, access: event.target.value as 'read' | 'write' } : item))}><option value="read">Read only</option><option value="write">Read and contribute</option></select>
          <button className="button ghost" disabled={busy} onClick={() => setMembers((previous) => previous.filter((item) => item.user_id !== member.user_id))}>Remove</button>
        </div>)}
      </>}
      <label>Reason for this change<input value={reason} maxLength={500} disabled={busy} onChange={(event) => setReason(event.target.value)} placeholder="Required for the audit record" /></label>
      <div><button className="button primary" disabled={busy || reason.trim().length < 5} onClick={() => void save()}>{busy ? 'Saving…' : 'Apply sharing'}</button><button className="button secondary" disabled={busy} onClick={() => { void api.threadAccess(threadId).then(load).catch((err: Error) => setError(err.message)) }}>Reload settings</button></div>
    </>}
  </section>
}
