import { KeyRound, LoaderCircle, LogOut, MonitorSmartphone, ShieldCheck, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { ErrorBanner, PageHeader, StatusPill, formatRelative } from '../components/UI'
import { useAuth } from '../context/AuthContext'
import { api } from '../lib/api'
import type { AuthSession } from '../types'

export function ChangePasswordPage() {
  const { user, refreshUser, logout } = useAuth()
  const navigate = useNavigate()
  const forced = Boolean(user?.must_change_password)
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [sessions, setSessions] = useState<AuthSession[]>([])
  const [busy, setBusy] = useState(false)
  const [sessionBusy, setSessionBusy] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const loadSessions = async () => {
    if (forced) return
    try {
      setSessions((await api.ownSessions()).items)
    } catch (nextError) {
      setError((nextError as Error).message)
    }
  }

  useEffect(() => { void loadSessions() }, [forced])

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!user) return
    if (newPassword !== confirmPassword) {
      setError('The new passwords do not match.')
      return
    }
    setBusy(true)
    setError('')
    setSuccess('')
    try {
      await api.changePassword({
        current_password: currentPassword,
        new_password: newPassword,
        version: user.version,
      })
      await refreshUser()
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      if (forced) navigate('/playground', { replace: true })
      else setSuccess('Password changed. Other signed-in sessions were revoked.')
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy(false)
    }
  }

  async function revoke(session: AuthSession) {
    setSessionBusy(session.id)
    setError('')
    try {
      await api.revokeOwnSession(session.id)
      if (session.current) {
        await logout().catch(() => undefined)
        navigate('/login', { replace: true })
      } else {
        await loadSessions()
      }
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setSessionBusy('')
    }
  }

  async function revokeAll() {
    if (!window.confirm('Sign out every session, including this browser?')) return
    setSessionBusy('all')
    setError('')
    try {
      await api.revokeAllOwnSessions()
      await logout().catch(() => undefined)
      navigate('/login', { replace: true })
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setSessionBusy('')
    }
  }

  const passwordForm = <form className="security-password-form" onSubmit={(event) => void submit(event)}>
    {error && <ErrorBanner message={error} />}
    {success && <div className="success-banner security-success"><ShieldCheck size={16} />{success}</div>}
    <label>Current password<input type="password" autoComplete="current-password" autoFocus value={currentPassword} onChange={(event) => setCurrentPassword(event.target.value)} /></label>
    <label>New password<input type="password" autoComplete="new-password" value={newPassword} onChange={(event) => setNewPassword(event.target.value)} /><span className="field-hint">At least 8 characters with uppercase, lowercase, number, and symbol.</span></label>
    <label>Confirm new password<input type="password" autoComplete="new-password" value={confirmPassword} onChange={(event) => setConfirmPassword(event.target.value)} /></label>
    <button className="button primary security-submit" disabled={busy || !currentPassword || !newPassword || !confirmPassword}>{busy && <LoaderCircle className="spin" size={16} />} Change password</button>
  </form>

  if (forced) return <main className="login-page">
    <section className="login-card password-change-card">
      <div className="login-brand"><span><KeyRound size={22} /></span><div><strong>DeepAgent</strong><small>SECURITY CHECK</small></div></div>
      <div className="login-copy"><div className="login-lock"><ShieldCheck size={23} /></div><h1>Choose a new password</h1><p>Your account is using an initial or expired password. Change it before continuing.</p></div>
      {passwordForm}
      <button className="button ghost forced-logout" onClick={() => void logout()}><LogOut size={15} /> Sign out instead</button>
    </section>
  </main>

  return <div className="page-stack security-page">
    <PageHeader eyebrow="ACCOUNT SECURITY" title="Password & sessions" description="Change your password and review every browser currently signed in to your account." />
    <div className="security-account-grid">
      <section className="panel security-card"><div className="panel-heading"><div><h3>Change password</h3><p>Other sessions are signed out after a successful change.</p></div><KeyRound size={20} /></div>{passwordForm}</section>
      <section className="panel sessions-card"><div className="panel-heading"><div><h3>Signed-in sessions</h3><p>Revoke a device immediately if you do not recognize it.</p></div><button className="button danger" disabled={sessionBusy === 'all' || !sessions.some((session) => session.status === 'ACTIVE')} onClick={() => void revokeAll()}><Trash2 size={15} /> Sign out all</button></div><div className="session-list">{sessions.map((session) => <div className="session-row" key={session.id}><div className="session-device"><span><MonitorSmartphone size={18} /></span><div><strong>{session.current ? 'This browser' : session.user_agent?.split(' ').slice(0, 3).join(' ') || 'Unknown device'}</strong><small>{session.ip_address || 'Unknown IP'} · Last seen {formatRelative(session.last_seen_at)}</small></div></div><StatusPill status={session.current && session.status === 'ACTIVE' ? 'CURRENT' : session.status} /><button className="button secondary" disabled={session.status !== 'ACTIVE' || sessionBusy === session.id} onClick={() => void revoke(session)}>{sessionBusy === session.id ? <LoaderCircle className="spin" size={14} /> : <LogOut size={14} />} Revoke</button></div>)}{!sessions.length && <div className="session-empty">No session records are available.</div>}</div></section>
    </div>
  </div>
}
