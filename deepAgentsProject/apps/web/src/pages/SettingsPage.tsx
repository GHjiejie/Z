import { useEffect, useMemo, useState } from 'react'
import { CheckCircle2, CircleSlash2, LoaderCircle, Save, ServerCog, ShieldCheck, Sparkles, UserRound } from 'lucide-react'
import { ErrorBanner, LoadingBlock, PageHeader, StatusPill } from '../components/UI'
import { ProductionRoutingPanel } from '../components/ProductionRoutingPanel'
import { usePlatform } from '../context/PlatformContext'
import { api } from '../lib/api'
import type { Deployment, IntentRoutingProfile, PrimaryIntent } from '../types'

function pretty(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

export function SettingsPage() {
  const { context, loading, error } = usePlatform()
  const [routingProfile, setRoutingProfile] = useState<IntentRoutingProfile | null>(null)
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [routingLoading, setRoutingLoading] = useState(false)
  const [routingSaving, setRoutingSaving] = useState(false)
  const [routingError, setRoutingError] = useState('')
  const [routingNotice, setRoutingNotice] = useState('')

  useEffect(() => {
    if (!context?.features.routing_management) return
    let active = true
    setRoutingLoading(true)
    Promise.all([api.routingProfile(), api.deployments()])
      .then(([profile, deploymentResult]) => {
        if (!active) return
        const environment = context.environment.id.replace(/^env_/, '')
        setRoutingProfile(profile)
        setDeployments(deploymentResult.items.filter((item) => item.status === 'ACTIVE' && item.environment === environment))
        setRoutingError('')
      })
      .catch((nextError) => active && setRoutingError((nextError as Error).message))
      .finally(() => active && setRoutingLoading(false))
    return () => { active = false }
  }, [context?.environment.id, context?.features.routing_management, context?.project.id, context?.tenant.id])

  const canManageRouting = useMemo(
    () => context?.environment.id !== 'env_production' && !!context?.user.permissions.includes('routing.manage'),
    [context?.environment.id, context?.user.permissions],
  )

  function updateRoutingConfig<K extends keyof IntentRoutingProfile['config']>(key: K, value: IntentRoutingProfile['config'][K]) {
    setRoutingNotice('')
    setRoutingProfile((current) => current ? { ...current, config: { ...current.config, [key]: value } } : current)
  }

  function updateRoutingTarget(intent: PrimaryIntent, deploymentId: string) {
    if (intent === 'ambiguous') return
    const value = deploymentId || null
    updateRoutingConfig('target_deployments', {
      ...(routingProfile?.config.target_deployments ?? { coding: null, release: null, knowledge: null, general: null }),
      [intent]: value,
    })
  }

  async function saveRoutingProfile() {
    if (!routingProfile || !canManageRouting) return
    setRoutingSaving(true)
    setRoutingError('')
    setRoutingNotice('')
    try {
      const updated = await api.updateRoutingProfile({
        mode: routingProfile.mode,
        auto_route_threshold: routingProfile.config.auto_route_threshold,
        confirmation_threshold: routingProfile.config.confirmation_threshold,
        decision_ttl_seconds: routingProfile.config.decision_ttl_seconds,
        target_deployments: routingProfile.config.target_deployments,
      })
      setRoutingProfile(updated)
      setRoutingNotice(`Routing revision ${updated.revision_number} is now active.`)
    } catch (nextError) {
      setRoutingError((nextError as Error).message)
    } finally {
      setRoutingSaving(false)
    }
  }

  if (loading) return <LoadingBlock label="Loading project context…" />
  if (!context) return <ErrorBanner message={error || 'Project context is unavailable.'} />

  const nonCodingDeployments = deployments.filter((item) => !item.coding_enabled)
  const codingDeployments = deployments.filter((item) => item.coding_enabled)
  const knowledgeDeployments = deployments.filter((item) => item.knowledge_enabled)

  const routeSelect = (intent: Exclude<PrimaryIntent, 'ambiguous'>, options: Deployment[], optional = false) => <label>
    <span>{pretty(intent)} intent</span>
    <select
      value={routingProfile?.config.target_deployments[intent] ?? ''}
      onChange={(event) => updateRoutingTarget(intent, event.target.value)}
      disabled={!canManageRouting || routingSaving}
    >
      <option value="">{optional ? 'No dedicated target (use fallback)' : 'Select an active deployment…'}</option>
      {options.map((item) => <option key={item.id} value={item.id}>{item.agent_name ?? item.name} · {item.environment}</option>)}
    </select>
  </label>

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
    {context.features.routing_management && <section className="panel routing-settings">
      <div className="panel-heading">
        <div><h3>Intent routing</h3><p>Classify the first request, choose a compatible Agent deployment, then keep that deployment pinned for the conversation.</p></div>
        {routingProfile && <div className="routing-revision"><StatusPill status={routingProfile.mode} /><span>Revision {routingProfile.revision_number}</span></div>}
      </div>
      {routingLoading ? <LoadingBlock label="Loading intent routing profile…" /> : <>
        {routingError && <ErrorBanner message={routingError} />}
        {routingNotice && <div className="success-banner routing-save-notice"><CheckCircle2 size={17} /><span>{routingNotice}</span></div>}
        {routingProfile && <>
          <div className="routing-mode-row">
            <div className="routing-mode-copy"><Sparkles size={19} /><div><strong>Routing mode</strong><span>Active routes automatically. Shadow records a prediction while using the general Agent. Disabled always uses the general Agent.</span></div></div>
            <select
              aria-label="Intent routing mode"
              value={routingProfile.mode}
              disabled={!canManageRouting || routingSaving}
              onChange={(event) => {
                setRoutingNotice('')
                setRoutingProfile({ ...routingProfile, mode: event.target.value as IntentRoutingProfile['mode'] })
              }}
            >
              <option value="active">Active</option>
              <option value="shadow">Shadow</option>
              <option value="disabled">Disabled</option>
            </select>
          </div>
          <div className="routing-settings-grid">
            <div className="routing-targets">
              <h4>Intent targets</h4>
              <p>Only deployments with the required immutable capabilities are eligible.</p>
              <div className="routing-target-grid">
                {routeSelect('coding', codingDeployments)}
                {routeSelect('release', nonCodingDeployments)}
                {routeSelect('knowledge', knowledgeDeployments, true)}
                {routeSelect('general', nonCodingDeployments)}
              </div>
            </div>
            <div className="routing-thresholds">
              <h4>Confidence policy</h4>
              <p>Values are applied to new conversations only.</p>
              <label><span>Automatic route threshold</span><div><input type="number" min="0" max="1" step="0.05" value={routingProfile.config.auto_route_threshold} disabled={!canManageRouting || routingSaving} onChange={(event) => updateRoutingConfig('auto_route_threshold', Number(event.target.value))} /><small>{Math.round(routingProfile.config.auto_route_threshold * 100)}%</small></div></label>
              <label><span>Low-confidence threshold</span><div><input type="number" min="0" max="1" step="0.05" value={routingProfile.config.confirmation_threshold} disabled={!canManageRouting || routingSaving} onChange={(event) => updateRoutingConfig('confirmation_threshold', Number(event.target.value))} /><small>{Math.round(routingProfile.config.confirmation_threshold * 100)}%</small></div></label>
              <label><span>Decision validity</span><div><input type="number" min="60" max="3600" step="60" value={routingProfile.config.decision_ttl_seconds} disabled={!canManageRouting || routingSaving} onChange={(event) => updateRoutingConfig('decision_ttl_seconds', Number(event.target.value))} /><small>seconds</small></div></label>
            </div>
          </div>
          <div className="routing-settings-actions">
            <span>{context.environment.id === 'env_production' ? 'Production changes require independent approval in Production routing reviews below.' : canManageRouting ? 'Saving creates an immutable routing revision.' : 'Routing management permission is required.'}</span>
            <button className="button primary" disabled={!canManageRouting || routingSaving || routingProfile.config.confirmation_threshold > routingProfile.config.auto_route_threshold} onClick={() => void saveRoutingProfile()}>
              {routingSaving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />} Save routing profile
            </button>
          </div>
        </>}
      </>}
    </section>}
    {context.features.routing_management && <ProductionRoutingPanel />}
  </div>
}
