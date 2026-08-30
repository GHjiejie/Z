import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { Layout } from './components/Layout'
import { AuthProvider, useAuth } from './context/AuthContext'
import { PlatformProvider } from './context/PlatformContext'
import { AdvancedPage } from './pages/AdvancedPage'
import { AgentsPage } from './pages/AgentsPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { CodingWorkbenchPage } from './pages/CodingWorkbenchPage'
import { ChangePasswordPage } from './pages/ChangePasswordPage'
import { DashboardPage } from './pages/DashboardPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { LoginPage } from './pages/LoginPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { RunsPage } from './pages/RunsPage'
import { SettingsPage } from './pages/SettingsPage'
import { UsersPage } from './pages/UsersPage'

function RequireAuth() {
  const { user, loading } = useAuth()
  const location = useLocation()
  if (loading) return <div className="app-loading"><div className="brand-mark">D</div><span>Loading DeepAgent…</span></div>
  if (!user) return <Navigate to="/login" replace state={{ from: `${location.pathname}${location.search}` }} />
  return <Outlet />
}

function RequireUserManager() {
  const { user } = useAuth()
  return user?.is_super_admin || user?.roles.includes('tenant_admin') ? <Outlet /> : <Navigate to="/advanced" replace />
}

function RequirePasswordReady() {
  const { user } = useAuth()
  return user?.must_change_password ? <Navigate to="/change-password" replace /> : <Outlet />
}

function PlatformShell() {
  return <PlatformProvider><Layout /></PlatformProvider>
}

function ApplicationRoutes() {
  return <Routes>
    <Route path="/login" element={<LoginPage />} />
    <Route element={<RequireAuth />}>
      <Route path="change-password" element={<ChangePasswordPage />} />
      <Route element={<RequirePasswordReady />}>
        <Route element={<PlatformShell />}>
        <Route index element={<Navigate to="/playground" replace />} />
        <Route path="security" element={<ChangePasswordPage />} />
        <Route path="playground" element={<PlaygroundPage />} />
        <Route path="knowledge" element={<KnowledgePage />} />
        <Route path="advanced" element={<AdvancedPage />} />
        <Route path="advanced/overview" element={<DashboardPage />} />
        <Route path="advanced/agents" element={<AgentsPage />} />
        <Route path="advanced/coding" element={<CodingWorkbenchPage />} />
        <Route path="advanced/runs" element={<RunsPage />} />
        <Route path="advanced/runs/:runId" element={<RunsPage />} />
        <Route path="advanced/approvals" element={<ApprovalsPage />} />
        <Route path="advanced/resources" element={<ResourcesPage />} />
        <Route path="advanced/settings" element={<SettingsPage />} />
        <Route element={<RequireUserManager />}><Route path="advanced/users" element={<UsersPage />} /></Route>
        <Route path="agents" element={<Navigate to="/advanced/agents" replace />} />
        <Route path="coding" element={<Navigate to="/advanced/coding" replace />} />
        <Route path="runs" element={<Navigate to="/advanced/runs" replace />} />
        <Route path="runs/:runId" element={<LegacyRunRedirect />} />
        <Route path="approvals" element={<Navigate to="/advanced/approvals" replace />} />
        <Route path="resources" element={<Navigate to="/advanced/resources" replace />} />
        <Route path="settings" element={<Navigate to="/advanced/settings" replace />} />
        <Route path="*" element={<Navigate to="/playground" replace />} />
        </Route>
      </Route>
    </Route>
  </Routes>
}

function LegacyRunRedirect() {
  const location = useLocation()
  return <Navigate to={`/advanced${location.pathname}${location.search}`} replace />
}

export default function App() {
  return <AuthProvider><ApplicationRoutes /></AuthProvider>
}
