import { CheckCircle2, CircleSlash2, ServerCog, ShieldCheck, UserRound } from 'lucide-react'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill } from '../components/UI'
import { usePlatform } from '../context/PlatformContext'

function pretty(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function SettingsPage() {
  const { context, loading, error } = usePlatform()
  if (loading) return <LoadingBlock label="Loading project context…" />
  if (!context) return <ErrorBanner message={error || 'Project context is unavailable.'} />

  return <div className="page-stack settings-page">
    <PageHeader eyebrow="PROJECT SETTINGS" title="Current project context" description="This reference deployment exposes the authoritative request scope and supported console capabilities." />
    <section className="settings-grid">
      <div className="panel settings-card"><UserRound size={20} /><span>User</span><strong>{context.user.name}</strong><small>{context.user.role} · {context.user.id}</small></div>
      <div className="panel settings-card"><ShieldCheck size={20} /><span>Tenant</span><strong>{context.tenant.name}</strong><small>{context.tenant.id}</small></div>
      <div className="panel settings-card"><ServerCog size={20} /><span>Project</span><strong>{context.project.name}</strong><small>{context.environment.name}</small></div>
    </section>
    <section className="panel capability-matrix">
      <div className="panel-heading"><div><h3>Console capabilities</h3><p>Unavailable features are intentionally hidden elsewhere in the console.</p></div><StatusPill status={context.runtime.status} /></div>
      <div className="capability-matrix-list">
        {Object.entries(context.features).map(([name, available]) => <div key={name}>
          {available ? <CheckCircle2 className="available" size={18} /> : <CircleSlash2 size={18} />}
          <span>{pretty(name)}</span><strong>{available ? 'Available' : 'Not available'}</strong>
        </div>)}
      </div>
    </section>
  </div>
}
