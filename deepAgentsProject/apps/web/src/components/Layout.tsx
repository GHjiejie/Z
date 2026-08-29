import {
  Activity,
  Bell,
  Blocks,
  Bot,
  ChevronDown,
  Command,
  FileCheck2,
  LibraryBig,
  LayoutDashboard,
  Menu,
  PlayCircle,
  Search,
  Settings,
  Sparkles,
  X,
} from 'lucide-react'
import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'

const nav = [
  { to: '/', label: 'Overview', icon: LayoutDashboard },
  { to: '/agents', label: 'Agents', icon: Bot },
  { to: '/playground', label: 'Playground', icon: PlayCircle },
  { to: '/knowledge', label: 'Knowledge', icon: LibraryBig },
  { to: '/runs', label: 'Runs & traces', icon: Activity },
  { to: '/approvals', label: 'Approvals', icon: FileCheck2, badge: true },
  { to: '/resources', label: 'Resources', icon: Blocks },
]

const titles: Record<string, string> = {
  '/': 'Platform overview',
  '/agents': 'Agent registry',
  '/playground': 'Agent playground',
  '/knowledge': 'Knowledge & retrieval',
  '/runs': 'Runs & traces',
  '/approvals': 'Approval center',
  '/resources': 'Resource registry',
}

export function Layout() {
  const [mobileOpen, setMobileOpen] = useState(false)
  const location = useLocation()

  return (
    <div className="app-shell">
      <aside className={`sidebar ${mobileOpen ? 'mobile-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark"><Sparkles size={18} strokeWidth={2.4} /></div>
          <div>
            <strong>DeepAgent</strong>
            <span>PLATFORM</span>
          </div>
          <button className="icon-button sidebar-close" onClick={() => setMobileOpen(false)}><X size={18} /></button>
        </div>

        <div className="workspace-switcher">
          <div className="workspace-avatar">A</div>
          <div className="workspace-copy">
            <span>WORKSPACE</span>
            <strong>Atlas Engineering</strong>
          </div>
          <ChevronDown size={15} />
        </div>

        <nav className="nav-list" aria-label="Primary">
          <span className="nav-eyebrow">CONTROL CENTER</span>
          {nav.map(({ to, label, icon: Icon, badge }) => (
            <NavLink
              key={to}
              to={to}
              end={to === '/'}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
            >
              <Icon size={18} />
              <span>{label}</span>
              {badge && <i className="nav-dot" />}
            </NavLink>
          ))}
        </nav>

        <div className="sidebar-bottom">
          <div className="system-card">
            <div className="system-card-head"><span className="health-dot" /> Runtime healthy</div>
            <div className="system-card-row"><span>Worker pool</span><strong>1 / 1</strong></div>
            <div className="system-card-row"><span>Event lag</span><strong>18ms</strong></div>
          </div>
          <a className="nav-item" href="#settings"><Settings size={18} /><span>Settings</span></a>
          <div className="user-card">
            <div className="user-avatar">ZJ</div>
            <div><strong>Zhengjie</strong><span>Workspace owner</span></div>
            <ChevronDown size={15} />
          </div>
        </div>
      </aside>

      {mobileOpen && <button className="sidebar-scrim" onClick={() => setMobileOpen(false)} />}

      <main className="main-column">
        <header className="topbar">
          <div className="topbar-title">
            <button className="icon-button mobile-menu" onClick={() => setMobileOpen(true)}><Menu size={20} /></button>
            <div>
              <span className="breadcrumb">Atlas Engineering <b>/</b> Project Atlas</span>
              <h1>{titles[location.pathname] ?? 'DeepAgent Platform'}</h1>
            </div>
          </div>
          <div className="topbar-actions">
            <button className="search-button"><Search size={17} /><span>Search anything</span><kbd><Command size={12} /> K</kbd></button>
            <button className="icon-button notification"><Bell size={18} /><i /></button>
            <div className="environment-pill"><span /> Development <ChevronDown size={14} /></div>
          </div>
        </header>
        <div className="page-content"><Outlet /></div>
      </main>
    </div>
  )
}
