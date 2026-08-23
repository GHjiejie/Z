import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { AgentsPage } from './pages/AgentsPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { DashboardPage } from './pages/DashboardPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { ResourcesPage } from './pages/ResourcesPage'
import { RunsPage } from './pages/RunsPage'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<DashboardPage />} />
        <Route path="agents" element={<AgentsPage />} />
        <Route path="playground" element={<PlaygroundPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="resources" element={<ResourcesPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

