import type { LucideIcon } from 'lucide-react'
import { AlertCircle, CheckCircle2, Clock3, LoaderCircle, PauseCircle, XCircle } from 'lucide-react'
import type { ReactNode } from 'react'

export function PageHeader({ eyebrow, title, description, actions }: { eyebrow?: string; title: string; description: string; actions?: ReactNode }) {
  return (
    <div className="page-header">
      <div>
        {eyebrow && <span className="page-eyebrow">{eyebrow}</span>}
        <h2>{title}</h2>
        <p>{description}</p>
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  )
}

export function StatusPill({ status }: { status: string }) {
  const normalized = status.toLowerCase()
  const icon = normalized.includes('succeed') || normalized === 'active' || normalized === 'healthy'
    ? <CheckCircle2 size={13} />
    : normalized.includes('fail') || normalized.includes('cancel') || normalized === 'unhealthy'
      ? <XCircle size={13} />
      : normalized.includes('wait') || normalized.includes('approval')
        ? <PauseCircle size={13} />
        : normalized.includes('run') || normalized.includes('prepar') || normalized.includes('resum')
          ? <LoaderCircle size={13} className="spin" />
          : <Clock3 size={13} />
  return <span className={`status-pill status-${normalized.replaceAll('_', '-').replaceAll(' ', '-')}`}>{icon}{status.replaceAll('_', ' ')}</span>
}

export function MetricCard({ label, value, detail, icon: Icon, tone = 'violet', delta }: { label: string; value: string | number; detail: string; icon: LucideIcon; tone?: string; delta?: string }) {
  return (
    <div className="metric-card">
      <div className={`metric-icon tone-${tone}`}><Icon size={20} /></div>
      <div className="metric-body"><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
      {delta && <span className="metric-delta">{delta}</span>}
    </div>
  )
}

export function EmptyState({ icon: Icon, title, description, action }: { icon: LucideIcon; title: string; description: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-icon"><Icon size={24} /></div>
      <h3>{title}</h3><p>{description}</p>{action}
    </div>
  )
}

export function LoadingBlock({ label = 'Loading platform data…' }: { label?: string }) {
  return <div className="loading-block"><LoaderCircle className="spin" size={21} /><span>{label}</span></div>
}

export function ErrorBanner({ message }: { message: string }) {
  return <div className="error-banner"><AlertCircle size={17} /><span>{message}</span></div>
}

export function formatRelative(date: string) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(date).getTime()) / 1000))
  if (seconds < 10) return 'just now'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}

export function shortId(id: string, length = 8) {
  const parts = id.split('_')
  return parts.length > 1 ? `${parts[0]}_${parts[1].slice(0, length)}` : id.slice(0, length)
}

