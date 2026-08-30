import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api, ApiError } from '../lib/api'
import type { PlatformUser } from '../types'

interface AuthState {
  user: PlatformUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<PlatformUser>
  logout: () => Promise<void>
  refreshUser: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<PlatformUser | null>(null)
  const [loading, setLoading] = useState(true)

  const refreshUser = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) throw error
      setUser(null)
    }
  }, [])

  useEffect(() => {
    refreshUser().catch(() => setUser(null)).finally(() => setLoading(false))
  }, [refreshUser])

  useEffect(() => {
    const unauthorized = () => setUser(null)
    window.addEventListener('deepagent:unauthorized', unauthorized)
    return () => window.removeEventListener('deepagent:unauthorized', unauthorized)
  }, [])

  const login = useCallback(async (username: string, password: string) => {
    const session = await api.login(username, password)
    setUser(session.user)
    return session.user
  }, [])

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } finally {
      setUser(null)
    }
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, logout, refreshUser }),
    [user, loading, login, logout, refreshUser],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used within AuthProvider')
  return value
}
