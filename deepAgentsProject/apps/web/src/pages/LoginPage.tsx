import { LoaderCircle, LockKeyhole, Sparkles } from 'lucide-react'
import { useState } from 'react'
import type { FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

export function LoginPage() {
  const { user, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  if (user) return <Navigate to={user.must_change_password ? '/change-password' : (location.state as { from?: string } | null)?.from ?? '/playground'} replace />

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const signedInUser = await login(username, password)
      const target = (location.state as { from?: string } | null)?.from ?? '/playground'
      navigate(signedInUser.must_change_password ? '/change-password' : target, { replace: true })
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return <main className="login-page">
    <section className="login-card">
      <div className="login-brand"><span><Sparkles size={22} /></span><div><strong>DeepAgent</strong><small>PLATFORM CONSOLE</small></div></div>
      <div className="login-copy"><div className="login-lock"><LockKeyhole size={23} /></div><h1>Welcome back</h1><p>Sign in to access your Playground, knowledge bases, and platform controls.</p></div>
      <form onSubmit={(event) => void submit(event)}>
        {error && <div className="login-error" role="alert">{error}</div>}
        <label>Username<input autoComplete="username" autoFocus value={username} onChange={(event) => setUsername(event.target.value)} placeholder="Enter your username" /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Enter your password" /></label>
        <button className="button primary login-submit" disabled={busy || !username || !password} type="submit">{busy && <LoaderCircle className="spin" size={16} />} Sign in</button>
      </form>
      <p className="login-footer">Protected by a server-managed session. Your password is never stored in browser-readable storage.</p>
    </section>
  </main>
}
