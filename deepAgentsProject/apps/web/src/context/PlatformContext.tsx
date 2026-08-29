import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from '../lib/api'
import type { Overview, PlatformContext as PlatformContextValue } from '../types'

interface PlatformState {
  context: PlatformContextValue | null
  overview: Overview | null
  loading: boolean
  error: string
  refresh: () => Promise<void>
}

const PlatformContext = createContext<PlatformState | null>(null)

export function PlatformProvider({ children }: { children: ReactNode }) {
  const [context, setContext] = useState<PlatformContextValue | null>(null)
  const [overview, setOverview] = useState<Overview | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async () => {
    setError('')
    try {
      const [nextContext, nextOverview] = await Promise.all([api.context(), api.overview()])
      setContext(nextContext)
      setOverview(nextOverview)
    } catch (nextError) {
      setError((nextError as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const value = useMemo(
    () => ({ context, overview, loading, error, refresh }),
    [context, overview, loading, error, refresh],
  )

  return <PlatformContext.Provider value={value}>{children}</PlatformContext.Provider>
}

export function usePlatform() {
  const value = useContext(PlatformContext)
  if (!value) throw new Error('usePlatform must be used within PlatformProvider')
  return value
}
