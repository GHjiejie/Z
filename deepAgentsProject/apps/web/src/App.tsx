import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { AgentsPage } from './pages/AgentsPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { DashboardPage } from './pages/DashboardPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { KnowledgePage } from './pages/KnowledgePage'
import { ResourcesPage } from './pages/ResourcesPage'
import { RunsPage } from './pages/RunsPage'
import { SettingsPage } from './pages/SettingsPage'
import { CodingWorkbenchPage } from './pages/CodingWorkbenchPage'
import { PlatformProvider } from './context/PlatformContext'

export default function App() {
  return (
    <PlatformProvider>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<DashboardPage />} />
          <Route path="agents" element={<AgentsPage />} />
          <Route path="playground" element={<PlaygroundPage />} />
          <Route path="coding" element={<CodingWorkbenchPage />} />
          <Route path="knowledge" element={<KnowledgePage />} />
          <Route path="runs" element={<RunsPage />} />
          <Route path="runs/:runId" element={<RunsPage />} />
          <Route path="approvals" element={<ApprovalsPage />} />
          <Route path="resources" element={<ResourcesPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </PlatformProvider>
  )
}
