import {
  Activity,
  Blocks,
  Bot,
  ChevronDown,
  CodeXml,
  Command,
  FileCheck2,
  LibraryBig,
  LayoutDashboard,
  KeyRound,
  LogOut,
  Menu,
  PlayCircle,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  UsersRound,
  X,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { usePlatform } from '../context/PlatformContext'
import { api } from '../lib/api'

const primaryNav = [
  { to: '/playground', label: 'Playground', icon: PlayCircle },
  { to: '/knowledge', label: 'Knowledge Base', icon: LibraryBig },
]

const advancedNav = [
  { to: '/advanced/overview', label: 'Overview', icon: LayoutDashboard },
  { to: '/advanced/agents', label: 'Agents', icon: Bot },
  { to: '/advanced/releases', label: 'Production releases', icon: FileCheck2 },
  { to: '/advanced/coding', label: 'Coding Workbench', icon: CodeXml },
  { to: '/advanced/runs', label: 'Runs', icon: Activity },
  { to: '/advanced/approvals', label: 'Approvals', icon: FileCheck2 },
  { to: '/advanced/resources', label: 'Resources', icon: Blocks },
  { to: '/advanced/settings', label: 'Settings', icon: Settings },
]

const titles: Record<string, string> = {
  '/playground': 'Playground',
  '/knowledge': 'Knowledge Base',
  '/advanced': 'Advanced features',
  '/advanced/overview': 'Platform overview',
  '/advanced/agents': 'Agent registry',
  '/advanced/releases': 'Production releases',
  '/advanced/coding': 'Coding workbench',
  '/advanced/runs': 'Runs & traces',
  '/advanced/approvals': 'Approval center',
  '/advanced/resources': 'Resource registry',
  '/advanced/users': 'User management',
  '/advanced/settings': 'Project settings',
  '/security': 'Password & sessions',
}

function label(value?: string, fallback = 'Unavailable') {
  if (!value) return fallback
  return value.replace(/^env_/, '').replaceAll('_', ' ').replace(/\b\w/g, (letter) => letter.toUpperCase())
}

interface SearchItem { id: string; title: string; subtitle: string; type: string; to: string }

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const location = useLocation()
  const { context, overview } = usePlatform()
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const advancedOpen = location.pathname.startsWith('/advanced')
  const pageTitle = location.pathname.startsWith('/advanced/runs/') ? 'Run inspector' : titles[location.pathname] ?? 'DeepAgent Platform'

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && context?.features.global_search) { event.preventDefault(); setSearchOpen(true) }
      if (event.key === 'Escape') { setSearchOpen(false); setMobileOpen(false) }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [context?.features.global_search])

  return <div className="app-shell">
    <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`} aria-label="Primary navigation">
      <div className="brand"><div className="brand-mark"><Sparkles size={18} strokeWidth={2.4} /></div><div><strong>DeepAgent</strong><span>PLATFORM</span></div><button aria-label="Close navigation" className="icon-button sidebar-close" onClick={() => setMobileOpen(false)}><X size={18} /></button></div>
      <div className="workspace-context"><div className="workspace-avatar">{label(context?.project.name, 'P').slice(0, 1)}</div><div className="workspace-copy"><span>PROJECT</span><strong>{label(context?.project.name)}</strong><small>{label(context?.tenant.name)}</small></div></div>
      <nav className="nav-list" aria-label="Primary">
        <div className="nav-group"><span className="nav-eyebrow">WORK</span>{primaryNav.map(({ to, label: itemLabel, icon: Icon }) => <NavLink key={to} to={to} onClick={() => setMobileOpen(false)} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Icon size={18} /><span>{itemLabel}</span></NavLink>)}</div>
        <div className="nav-group advanced-nav"><span className="nav-eyebrow">PLATFORM</span><NavLink to="/advanced" end className={({ isActive }) => `nav-item advanced-root ${isActive || advancedOpen ? 'active' : ''}`}><SlidersHorizontal size={18} /><span>Advanced features</span><ChevronDown className={advancedOpen ? 'open' : ''} size={15} /></NavLink>{advancedOpen && <div className="nav-sublist">{advancedNav.map(({ to, label: itemLabel, icon: Icon }) => <NavLink key={to} to={to} onClick={() => setMobileOpen(false)} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><Icon size={16} /><span>{itemLabel}</span>{to === '/advanced/approvals' && !!overview?.pending_approvals && <i className="nav-count">{overview.pending_approvals}</i>}</NavLink>)}{(user?.is_super_admin || user?.roles.includes('tenant_admin')) && <NavLink to="/advanced/users" onClick={() => setMobileOpen(false)} className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}><UsersRound size={16} /><span>Users</span></NavLink>}</div>}</div>
      </nav>
      <div className="sidebar-bottom"><div className={`system-card runtime-${context?.runtime.status ?? 'unavailable'}`}><div className="system-card-head"><span className="health-dot" /> Runtime {context?.runtime.status ?? 'unavailable'}</div><div className="system-card-row"><span>Workers</span><strong>{context ? `${context.runtime.workers_online} / ${context.runtime.workers_total}` : '—'}</strong></div><div className="system-card-row"><span>Queue</span><strong>{context?.runtime.queue_depth ?? '—'}</strong></div></div><div className="user-card"><div className="user-avatar">{(user?.display_name ?? 'U').slice(0, 2).toUpperCase()}</div><div><strong>{user?.display_name ?? label(context?.user.name)}</strong><span>{user?.is_super_admin ? 'Super administrator' : user?.roles.join(', ')}</span></div><button title="Password and sessions" aria-label="Password and sessions" onClick={() => navigate('/security')}><KeyRound size={16} /></button><button title="Sign out" aria-label="Sign out" onClick={() => void logout()}><LogOut size={16} /></button></div></div>
    </aside>
    {mobileOpen && <button aria-label="Close navigation" className="sidebar-scrim" onClick={() => setMobileOpen(false)} />}
    <main className="main-column"><header className="topbar"><div className="topbar-title"><button aria-label="Open navigation" className="icon-button mobile-menu" onClick={() => setMobileOpen(true)}><Menu size={20} /></button><div><span className="breadcrumb">{advancedOpen ? 'Advanced features' : label(context?.project.name)} <b>/</b> {pageTitle}</span><h1>{pageTitle}</h1></div></div><div className="topbar-actions">{context?.features.global_search && <button className="search-button" onClick={() => setSearchOpen(true)}><Search size={17} /><span>Search platform</span><kbd><Command size={12} /> K</kbd></button>}<div className="environment-pill" aria-label={`Current environment ${label(context?.environment.name)}`}><span />{label(context?.environment.name)}</div></div></header><div className="page-content" tabIndex={-1}><Outlet /></div></main>
    {searchOpen && <CommandPalette onClose={() => setSearchOpen(false)} />}
  </div>
}

function CommandPalette({ onClose }: { onClose: () => void }) {
  const [query, setQuery] = useState('')
  const [items, setItems] = useState<SearchItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const navigate = useNavigate()
  useEffect(() => { Promise.all([api.agents(), api.runs(), api.knowledgeBases()]).then(([agents, runs, bases]) => { setItems([...agents.items.map((agent) => ({ id: agent.id, title: agent.name, subtitle: agent.description || agent.id, type: 'Agent', to: `/advanced/agents?agent=${agent.id}` })), ...runs.items.map((run) => ({ id: run.id, title: run.input, subtitle: `${run.agent_name ?? 'Agent'} · ${run.status}`, type: 'Run', to: `/advanced/runs/${run.id}` })), ...bases.items.map((base) => ({ id: base.id, title: base.name, subtitle: base.description || base.id, type: 'Knowledge', to: `/knowledge?base=${base.id}` }))]) }).catch((nextError) => setError((nextError as Error).message)).finally(() => setLoading(false)); window.setTimeout(() => inputRef.current?.focus(), 0) }, [])
  const visible = useMemo(() => { const needle = query.trim().toLowerCase(); return (needle ? items.filter((item) => `${item.id} ${item.title} ${item.subtitle} ${item.type}`.toLowerCase().includes(needle)) : items).slice(0, 12) }, [items, query])
  const open = (item: SearchItem) => { onClose(); navigate(item.to) }
  return <div className="command-backdrop" role="presentation" onMouseDown={onClose}><section className="command-palette" role="dialog" aria-modal="true" aria-label="Search platform" onMouseDown={(event) => event.stopPropagation()}><div className="command-input"><Search size={20} /><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search agents, runs, and knowledge…" onKeyDown={(event) => { if (event.key === 'Enter' && visible[0]) open(visible[0]) }} /><button aria-label="Close search" className="icon-button" onClick={onClose}><X size={17} /></button></div><div className="command-results">{loading ? <div className="command-empty">Loading searchable resources…</div> : error ? <div className="command-empty">Search is unavailable: {error}</div> : visible.length ? visible.map((item) => <button key={`${item.type}-${item.id}`} onClick={() => open(item)}><span>{item.type}</span><div><strong>{item.title}</strong><small>{item.subtitle}</small></div></button>) : <div className="command-empty">No results for “{query}”.</div>}</div></section></div>
}
