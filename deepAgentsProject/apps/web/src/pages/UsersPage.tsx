import {
  ChevronLeft,
  ChevronRight,
  History,
  KeyRound,
  Laptop,
  LoaderCircle,
  Pencil,
  Plus,
  Search,
  ShieldCheck,
  UserRoundCheck,
  UserRoundX,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill, formatRelative } from '../components/UI'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import type { AuthAuditEvent, AuthSession, PlatformUser, UserListResponse } from '../types'

type EditorState =
  | { mode: 'create' }
  | { mode: 'edit'; user: PlatformUser }
  | { mode: 'password'; user: PlatformUser }
  | null

const emptyResult: UserListResponse = { items: [], page: 1, page_size: 20, total: 0, pages: 0 }

export function UsersPage() {
  const { user: currentUser } = useAuth()
  const [tab, setTab] = useState<'accounts' | 'audit'>('accounts')
  const [result, setResult] = useState<UserListResponse>(emptyResult)
  const [searchInput, setSearchInput] = useState('')
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState<'ACTIVE' | 'INACTIVE' | 'ALL'>('ALL')
  const [sort, setSort] = useState('username:asc')
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [editor, setEditor] = useState<EditorState>(null)
  const [deactivating, setDeactivating] = useState<PlatformUser | null>(null)
  const [sessionsUser, setSessionsUser] = useState<PlatformUser | null>(null)
  const [busyId, setBusyId] = useState('')

  useEffect(() => {
    const timeout = window.setTimeout(() => { setPage(1); setQuery(searchInput.trim()) }, 250)
    return () => window.clearTimeout(timeout)
  }, [searchInput])

  const load = useCallback(async () => {
    const [sort_by, sort_order] = sort.split(':') as [
      'username' | 'display_name' | 'status' | 'created_at' | 'updated_at' | 'last_login_at',
      'asc' | 'desc',
    ]
    setLoading(true)
    try {
      setResult(await api.users({ page, page_size: 20, q: query, status, sort_by, sort_order }))
      setError('')
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setLoading(false)
    }
  }, [page, query, sort, status])

  useEffect(() => { if (tab === 'accounts') void load() }, [load, tab])

  async function reactivate(user: PlatformUser) {
    setBusyId(user.id)
    setError('')
    try {
      await api.updateUser(user.id, { status: 'ACTIVE', version: user.version })
      await load()
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusyId('')
    }
  }

  if (!currentUser) return null
  return <div className="page-stack users-page">
    <PageHeader
      eyebrow="ACCESS CONTROL"
      title="User management"
      description="Manage scoped accounts, password posture, active sessions, and a complete security audit trail."
      actions={tab === 'accounts' ? <button className="button primary" onClick={() => setEditor({ mode: 'create' })}><Plus size={16} /> Add user</button> : undefined}
    />
    <div className="users-tabs" role="tablist">
      <button className={tab === 'accounts' ? 'active' : ''} onClick={() => setTab('accounts')}><ShieldCheck size={15} /> Accounts</button>
      <button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}><History size={15} /> Audit trail</button>
    </div>
    {error && <ErrorBanner message={error} />}
    {tab === 'accounts' ? <section className="panel users-panel">
      <div className="users-toolbar">
        <div className="table-search"><Search size={16} /><input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} placeholder="Search users or scope…" /></div>
        <select aria-label="Filter users by status" value={status} onChange={(event) => { setStatus(event.target.value as typeof status); setPage(1) }}><option value="ALL">All statuses</option><option value="ACTIVE">Active</option><option value="INACTIVE">Inactive</option></select>
        <select aria-label="Sort users" value={sort} onChange={(event) => { setSort(event.target.value); setPage(1) }}><option value="username:asc">Username A–Z</option><option value="username:desc">Username Z–A</option><option value="updated_at:desc">Recently updated</option><option value="last_login_at:desc">Recent login</option><option value="created_at:desc">Newest accounts</option></select>
        <span>{result.total} accounts</span>
      </div>
      {loading ? <LoadingBlock label="Loading user accounts…" /> : <>
        <div className="table-wrap"><table className="data-table users-table"><thead><tr><th>User</th><th>Access</th><th>Scope</th><th>Status</th><th>Security</th><th>Last login</th><th>Actions</th></tr></thead><tbody>{result.items.map((user) => {
          const locked = Boolean(user.locked_until && new Date(user.locked_until) > new Date())
          return <tr key={user.id}><td><div className="managed-user"><span>{user.display_name.slice(0, 2).toUpperCase()}</span><div><strong>{user.display_name}</strong><small>@{user.username}{user.id === currentUser.id ? ' · You' : ''}</small></div></div></td><td><div className="user-roles">{user.is_super_admin && <span className="super-role"><ShieldCheck size={12} /> Super admin</span>}{user.roles.map((role) => <span key={role}>{role}</span>)}</div></td><td><strong className="table-strong">{user.project_id}</strong><span className="table-sub">{user.tenant_id} · {user.environment_id.replace(/^env_/, '')}</span></td><td><StatusPill status={user.status} />{user.deletion_reason && <span className="deletion-note" title={user.deletion_reason}>{user.deletion_reason}</span>}</td><td><div className="security-flags">{locked ? <StatusPill status="LOCKED" /> : user.must_change_password ? <StatusPill status="PASSWORD CHANGE" /> : <span>Ready</span>}</div></td><td><span className="table-time">{user.last_login_at ? formatRelative(user.last_login_at) : 'Never'}</span></td><td><div className="user-actions"><button title="Edit user" aria-label={`Edit ${user.username}`} onClick={() => setEditor({ mode: 'edit', user })}><Pencil size={15} /></button><button title="Reset password" aria-label={`Reset ${user.username} password`} onClick={() => setEditor({ mode: 'password', user })}><KeyRound size={15} /></button><button title="Manage sessions" aria-label={`Manage ${user.username} sessions`} onClick={() => setSessionsUser(user)}><Laptop size={15} /></button><button disabled={busyId === user.id || user.id === currentUser.id} title={user.status === 'ACTIVE' ? 'Disable user' : 'Activate user'} aria-label={`${user.status === 'ACTIVE' ? 'Disable' : 'Activate'} ${user.username}`} onClick={() => user.status === 'ACTIVE' ? setDeactivating(user) : void reactivate(user)}>{busyId === user.id ? <LoaderCircle className="spin" size={15} /> : user.status === 'ACTIVE' ? <UserRoundX size={15} /> : <UserRoundCheck size={15} />}</button></div></td></tr>
        })}{!result.items.length && <tr><td className="empty-cell" colSpan={7}>No users match these filters.</td></tr>}</tbody></table></div>
        <Pagination page={result.page} pages={result.pages} total={result.total} onPage={setPage} />
      </>}
    </section> : <AuditPanel />}
    {editor && <UserEditor state={editor} currentUser={currentUser} onClose={() => setEditor(null)} onSaved={() => { setEditor(null); void load() }} />}
    {deactivating && <DeactivateDialog user={deactivating} onClose={() => setDeactivating(null)} onSaved={() => { setDeactivating(null); void load() }} />}
    {sessionsUser && <SessionsDialog user={sessionsUser} onClose={() => setSessionsUser(null)} />}
  </div>
}

function Pagination({ page, pages, total, onPage }: { page: number; pages: number; total: number; onPage: (page: number) => void }) {
  if (!total) return null
  return <div className="table-pagination"><span>Page {page} of {Math.max(1, pages)}</span><div><button aria-label="Previous page" disabled={page <= 1} onClick={() => onPage(page - 1)}><ChevronLeft size={15} /></button><button aria-label="Next page" disabled={page >= pages} onClick={() => onPage(page + 1)}><ChevronRight size={15} /></button></div></div>
}

function UserEditor({ state, currentUser, onClose, onSaved }: { state: Exclude<EditorState, null>; currentUser: PlatformUser; onClose: () => void; onSaved: () => void }) {
  const target = state.mode === 'create' ? null : state.user
  const [username, setUsername] = useState(target?.username ?? '')
  const [displayName, setDisplayName] = useState(target?.display_name ?? '')
  const [password, setPassword] = useState('')
  const [roles, setRoles] = useState(target?.roles.join(', ') ?? 'member')
  const [tenantId, setTenantId] = useState(target?.tenant_id ?? currentUser.tenant_id)
  const [projectId, setProjectId] = useState(target?.project_id ?? currentUser.project_id)
  const [environmentId, setEnvironmentId] = useState(target?.environment_id ?? currentUser.environment_id)
  const [superAdmin, setSuperAdmin] = useState(target?.is_super_admin ?? false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const passwordOnly = state.mode === 'password'

  async function save(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (state.mode === 'create') {
        await api.createUser({ username, display_name: displayName, password, roles: roles.split(',').map((role) => role.trim()).filter(Boolean), tenant_id: tenantId, project_id: projectId, environment_id: environmentId, is_super_admin: currentUser.is_super_admin && superAdmin })
      } else if (state.mode === 'password') {
        await api.resetUserPassword(state.user.id, password, state.user.version)
      } else {
        await api.updateUser(state.user.id, { version: state.user.version, username, display_name: displayName, roles: roles.split(',').map((role) => role.trim()).filter(Boolean), tenant_id: tenantId, project_id: projectId, environment_id: environmentId, ...(currentUser.is_super_admin ? { is_super_admin: superAdmin } : {}) })
      }
      onSaved()
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <div className="modal-backdrop" onMouseDown={onClose}><section className="modal user-modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><span className="page-eyebrow">{state.mode === 'create' ? 'NEW ACCOUNT' : state.mode === 'password' ? 'SECURITY' : 'ACCOUNT DETAILS'}</span><h3>{state.mode === 'create' ? 'Add a platform user' : state.mode === 'password' ? `Reset @${state.user.username} password` : `Edit @${state.user.username}`}</h3><p>{passwordOnly ? 'All of this user’s sessions will be revoked and a password change is required at next sign-in.' : 'Changes use optimistic locking so another administrator’s updates cannot be overwritten.'}</p></div><button className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></div><form onSubmit={(event) => void save(event)}><div className="form-stack">{error && <ErrorBanner message={error} />}{passwordOnly ? <label>New temporary password<input type="password" autoFocus autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /><span className="field-hint">At least 8 characters with uppercase, lowercase, number, and symbol.</span></label> : <><div className="form-row"><label>Username<input autoFocus value={username} onChange={(event) => setUsername(event.target.value)} /></label><label>Display name<input value={displayName} onChange={(event) => setDisplayName(event.target.value)} /></label></div>{state.mode === 'create' && <label>Initial password<input type="password" autoComplete="new-password" value={password} onChange={(event) => setPassword(event.target.value)} /><span className="field-hint">The user must change this password at first sign-in.</span></label>}<label>Roles<input value={roles} onChange={(event) => setRoles(event.target.value)} placeholder="member, reviewer" /><span className="field-hint">Use tenant_admin to delegate management within this tenant.</span></label><div className="form-row"><label>Tenant<input disabled={!currentUser.is_super_admin} value={tenantId} onChange={(event) => setTenantId(event.target.value)} /></label><label>Project<input value={projectId} onChange={(event) => setProjectId(event.target.value)} /></label></div><label>Environment<input value={environmentId} onChange={(event) => setEnvironmentId(event.target.value)} /></label>{currentUser.is_super_admin && <label className="checkbox-field"><input type="checkbox" checked={superAdmin} disabled={target?.id === currentUser.id} onChange={(event) => setSuperAdmin(event.target.checked)} /><span><strong>Platform super administrator</strong><small>Can manage accounts across every tenant.</small></span></label>}</>}</div><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button primary" disabled={busy || (passwordOnly ? !password : !username || !displayName || (state.mode === 'create' && !password))}>{busy && <LoaderCircle className="spin" size={15} />} {state.mode === 'create' ? 'Create user' : passwordOnly ? 'Reset password' : 'Save changes'}</button></div></form></section></div>
}

function DeactivateDialog({ user, onClose, onSaved }: { user: PlatformUser; onClose: () => void; onSaved: () => void }) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  async function deactivate(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      await api.deactivateUser(user.id, user.version, reason)
      onSaved()
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy(false)
    }
  }
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="modal confirm-modal" role="alertdialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><span className="page-eyebrow danger-eyebrow">DEACTIVATE ACCOUNT</span><h3>Disable @{user.username}?</h3><p>This immediately revokes every active session. The account can be reactivated later.</p></div><button className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></div><form onSubmit={(event) => void deactivate(event)}><div className="confirm-body">{error && <ErrorBanner message={error} />}<label>Reason for deactivation<textarea rows={3} autoFocus value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Required for the audit trail" /></label></div><div className="modal-actions"><button type="button" className="button secondary" onClick={onClose}>Cancel</button><button className="button danger" disabled={busy || reason.trim().length < 3}>{busy && <LoaderCircle className="spin" size={15} />} Disable user</button></div></form></section></div>
}

function SessionsDialog({ user, onClose }: { user: PlatformUser; onClose: () => void }) {
  const [sessions, setSessions] = useState<AuthSession[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const load = useCallback(async () => {
    try {
      setSessions((await api.userSessions(user.id)).items)
      setError('')
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setLoading(false)
    }
  }, [user.id])
  useEffect(() => { void load() }, [load])
  async function revoke(sessionId: string) {
    setBusy(sessionId)
    try {
      const result = await api.revokeUserSession(user.id, sessionId)
      if (result.revoked_current) window.location.assign('/login')
      else await load()
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy('')
    }
  }
  async function revokeAll() {
    if (!window.confirm(`Sign out every active session for @${user.username}?`)) return
    setBusy('all')
    try {
      const result = await api.revokeAllUserSessions(user.id)
      if (result.revoked_current) window.location.assign('/login')
      else await load()
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy('')
    }
  }
  return <div className="modal-backdrop" onMouseDown={onClose}><section className="modal sessions-modal" role="dialog" aria-modal="true" onMouseDown={(event) => event.stopPropagation()}><div className="modal-heading"><div><span className="page-eyebrow">ACTIVE ACCESS</span><h3>@{user.username} sessions</h3><p>Revoked and expired sessions remain visible until routine cleanup.</p></div><button className="icon-button" aria-label="Close" onClick={onClose}><X size={18} /></button></div>{error && <div className="modal-inline-error"><ErrorBanner message={error} /></div>}{loading ? <LoadingBlock label="Loading sessions…" /> : <div className="session-list modal-session-list">{sessions.map((session) => <div className="session-row" key={session.id}><div className="session-device"><span><Laptop size={17} /></span><div><strong>{session.current ? 'Current administrator session' : session.user_agent?.split(' ').slice(0, 3).join(' ') || 'Unknown device'}</strong><small>{session.ip_address || 'Unknown IP'} · {formatRelative(session.last_seen_at)}</small></div></div><StatusPill status={session.current && session.status === 'ACTIVE' ? 'CURRENT' : session.status} /><button className="button secondary" disabled={session.status !== 'ACTIVE' || busy === session.id} onClick={() => void revoke(session.id)}>{busy === session.id ? <LoaderCircle className="spin" size={14} /> : 'Revoke'}</button></div>)}{!sessions.length && <div className="session-empty">No session records are available.</div>}</div>}<div className="modal-actions"><button className="button secondary" onClick={onClose}>Close</button><button className="button danger" disabled={busy === 'all' || !sessions.some((session) => session.status === 'ACTIVE')} onClick={() => void revokeAll()}>Revoke all active</button></div></section></div>
}

function AuditPanel() {
  const [events, setEvents] = useState<AuthAuditEvent[]>([])
  const [query, setQuery] = useState('')
  const [outcome, setOutcome] = useState('')
  const [page, setPage] = useState(1)
  const [pages, setPages] = useState(0)
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setLoading(true)
      api.userAuditEvents({ page, page_size: 20, q: query.trim(), outcome }).then((result) => { setEvents(result.items); setPages(result.pages); setTotal(result.total); setError('') }).catch((nextError) => setError((nextError as Error).message)).finally(() => setLoading(false))
    }, 200)
    return () => window.clearTimeout(timeout)
  }, [outcome, page, query])
  return <section className="panel users-panel audit-panel"><div className="users-toolbar"><div className="table-search"><Search size={16} /><input value={query} onChange={(event) => { setQuery(event.target.value); setPage(1) }} placeholder="Search action or user ID…" /></div><select aria-label="Filter audit outcome" value={outcome} onChange={(event) => { setOutcome(event.target.value); setPage(1) }}><option value="">All outcomes</option><option value="SUCCEEDED">Succeeded</option><option value="DENIED">Denied</option></select><span>{total} events</span></div>{error && <div className="audit-error"><ErrorBanner message={error} /></div>}{loading ? <LoadingBlock label="Loading audit trail…" /> : <><div className="table-wrap"><table className="data-table"><thead><tr><th>Action</th><th>Outcome</th><th>Actor</th><th>Target</th><th>Scope</th><th>Details</th><th>Time</th></tr></thead><tbody>{events.map((event) => <tr key={event.id}><td><strong className="table-strong">{event.action.replaceAll('_', ' ')}</strong></td><td><StatusPill status={event.outcome} /></td><td><code>{event.actor_user_id || 'system'}</code></td><td><code>{event.target_user_id || '—'}</code></td><td><span>{event.tenant_id || '—'}</span><span className="table-sub">{event.project_id || '—'}</span></td><td><code className="audit-details">{Object.keys(event.details).length ? JSON.stringify(event.details) : '—'}</code></td><td><span className="table-time" title={event.created_at}>{formatRelative(event.created_at)}</span></td></tr>)}{!events.length && <tr><td className="empty-cell" colSpan={7}>No audit events match these filters.</td></tr>}</tbody></table></div><Pagination page={page} pages={pages} total={total} onPage={setPage} /></>}</section>
}
