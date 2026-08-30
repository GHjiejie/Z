import { Activity, ArrowRight, Blocks, Bot, CodeXml, FileCheck2, LayoutDashboard, Settings, UsersRound } from 'lucide-react'
import { Link } from 'react-router-dom'
import { PageHeader } from '../components/UI'
import { useAuth } from '../context/AuthContext'

const features = [
  { to: '/advanced/overview', icon: LayoutDashboard, title: 'Platform overview', description: 'Operational health, usage, recent runs, and items requiring attention.', tone: 'violet' },
  { to: '/advanced/agents', icon: Bot, title: 'Agent registry', description: 'Create, validate, publish, and deploy versioned Agent definitions.', tone: 'blue' },
  { to: '/advanced/coding', icon: CodeXml, title: 'Coding workbench', description: 'Run governed coding tasks against isolated repository snapshots.', tone: 'green' },
  { to: '/advanced/runs', icon: Activity, title: 'Runs & traces', description: 'Inspect execution events, artifacts, attempts, and usage evidence.', tone: 'cyan' },
  { to: '/advanced/approvals', icon: FileCheck2, title: 'Approval center', description: 'Review policy-gated actions and resume checkpointed work.', tone: 'amber' },
  { to: '/advanced/resources', icon: Blocks, title: 'Resource registry', description: 'Inspect models, plugins, and immutable Skill artifacts.', tone: 'indigo' },
  { to: '/advanced/settings', icon: Settings, title: 'Platform settings', description: 'Review capabilities and manage intent-routing revisions.', tone: 'slate' },
]

export function AdvancedPage() {
  const { user } = useAuth()
  const items = user?.is_super_admin
    ? [...features, { to: '/advanced/users', icon: UsersRound, title: 'User management', description: 'Create accounts, assign access, reset passwords, and disable users.', tone: 'rose' }]
    : features
  return <div className="page-stack advanced-page">
    <PageHeader eyebrow="ADVANCED FEATURES" title="Platform control center" description="Administrative, build, governance, and operational tools are grouped here. Playground and Knowledge remain the primary working areas." />
    <div className="advanced-grid">{items.map(({ to, icon: Icon, title, description, tone }) => <Link className="panel advanced-card" to={to} key={to}><div className={`summary-icon tone-${tone}`}><Icon size={20} /></div><div><strong>{title}</strong><span>{description}</span></div><ArrowRight size={17} /></Link>)}</div>
  </div>
}
