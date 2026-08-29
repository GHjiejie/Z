import type {
  Agent,
  AgentDraft,
  Deployment,
  Interrupt,
  KnowledgeBase,
  KnowledgeIngestionJob,
  KnowledgeRevision,
  KnowledgeSearchResult,
  KnowledgeUploadPreparation,
  ModelDeployment,
  Overview,
  PlatformContext,
  Plugin,
  Run,
  RunArtifact,
  RuntimeEvent,
  Skill,
  ThreadSummary,
} from '../types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

const tenantHeaders = {
  'X-Tenant-ID': 'tenant_demo',
  'X-Project-ID': 'project_atlas',
  'X-Environment-ID': 'env_development',
  'X-User-ID': 'user_demo',
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      ...tenantHeaders,
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...options.headers,
    },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body?.error?.message ?? body?.detail ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const api = {
  context: () => request<PlatformContext>('/api/v1/context'),
  overview: () => request<Overview>('/api/v1/overview'),
  agents: () => request<{ items: Agent[] }>('/api/v1/agents'),
  agent: (id: string) => request<Agent>(`/api/v1/agents/${id}`),
  createAgent: (body: { name: string; description: string; draft?: AgentDraft }) =>
    request<Agent>('/api/v1/agents', { method: 'POST', body: JSON.stringify(body) }),
  updateAgent: (id: string, body: { name: string; description: string; draft: AgentDraft; version: number }) =>
    request<Agent>(`/api/v1/agents/${id}/draft`, { method: 'PATCH', body: JSON.stringify(body) }),
  validateAgent: (id: string) =>
    request<{ valid: boolean; issues: Array<{ level: string; message: string }> }>(`/api/v1/agents/${id}/revisions:validate`, { method: 'POST' }),
  publishAgent: (id: string) =>
    request<{ revision: { id: string; revision_number: number }; resolved_plan: { id: string; plan_hash: string } }>(`/api/v1/agents/${id}/revisions:publish`, { method: 'POST' }),
  deployments: () => request<{ items: Deployment[] }>('/api/v1/agent-deployments'),
  deploy: (agentRevisionId: string, environment = 'development') =>
    request<Deployment>('/api/v1/agent-deployments', {
      method: 'POST',
      body: JSON.stringify({ agent_revision_id: agentRevisionId, environment }),
    }),
  models: () => request<{ items: ModelDeployment[] }>('/api/v1/models'),
  plugins: () => request<{ items: Plugin[] }>('/api/v1/plugins'),
  skills: () => request<{ items: Skill[] }>('/api/v1/skills'),
  knowledgeBases: () => request<{ items: KnowledgeBase[] }>('/api/v1/knowledge-bases'),
  knowledgeBase: (id: string) => request<KnowledgeBase>(`/api/v1/knowledge-bases/${id}`),
  createKnowledgeBase: (body: { name: string; description: string }) =>
    request<KnowledgeBase>('/api/v1/knowledge-bases', { method: 'POST', body: JSON.stringify(body) }),
  knowledgeRevisions: (knowledgeBaseId: string) =>
    request<{ items: KnowledgeRevision[] }>(`/api/v1/knowledge-bases/${knowledgeBaseId}/revisions`),
  prepareKnowledgeUpload: (knowledgeBaseId: string, file: File) =>
    request<KnowledgeUploadPreparation>(`/api/v1/knowledge-bases/${knowledgeBaseId}/documents:prepare-upload`, {
      method: 'POST',
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || 'application/octet-stream',
        size_bytes: file.size,
        visibility: 'project',
        allowed_roles: [],
      }),
    }),
  uploadKnowledgeFile: async (preparation: KnowledgeUploadPreparation, file: File) => {
    const platformUpload = preparation.upload.url.startsWith('/')
    const response = await fetch(platformUpload ? `${API_BASE}${preparation.upload.url}` : preparation.upload.url, {
      method: preparation.upload.method,
      body: file,
      headers: {
        ...(platformUpload ? tenantHeaders : {}),
        'Content-Type': file.type || 'application/octet-stream',
        ...preparation.upload.required_headers,
      },
    })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(body?.error?.message ?? body?.detail ?? `Upload failed (${response.status})`)
    }
    return { etag: response.headers.get('etag')?.replaceAll('"', '') ?? undefined }
  },
  completeKnowledgeUpload: (versionId: string, etag?: string) =>
    request<KnowledgeIngestionJob>(`/api/v1/knowledge-document-versions/${versionId}:complete`, {
      method: 'POST',
      body: JSON.stringify({ etag }),
    }),
  knowledgeJob: (id: string) => request<KnowledgeIngestionJob>(`/api/v1/knowledge-ingestion-jobs/${id}`),
  retryKnowledgeJob: (id: string) => request<KnowledgeIngestionJob>(`/api/v1/knowledge-ingestion-jobs/${id}:retry`, { method: 'POST' }),
  searchKnowledge: (knowledgeBaseId: string, query: string, topK = 8) =>
    request<KnowledgeSearchResult>('/api/v1/knowledge:search', {
      method: 'POST',
      body: JSON.stringify({ knowledge_base_id: knowledgeBaseId, query, top_k: topK }),
    }),
  threads: () => request<{ items: ThreadSummary[] }>('/api/v1/threads'),
  thread: (id: string) => request<ThreadSummary>(`/api/v1/threads/${id}`),
  runs: () => request<{ items: Run[] }>('/api/v1/runs'),
  run: (id: string) => request<Run>(`/api/v1/runs/${id}`),
  runEvents: (id: string, after = 0) =>
    request<{ items: RuntimeEvent[] }>(`/api/v1/runs/${id}/events?after_sequence=${after}`),
  runArtifacts: (id: string) => request<{ items: RunArtifact[] }>(`/api/v1/runs/${id}/artifacts`),
  runSpans: (id: string) => request<{ items: Array<Record<string, unknown>> }>(`/api/v1/runs/${id}/spans`),
  createThread: (deploymentId: string, title: string) =>
    request<ThreadSummary>('/api/v1/threads', {
      method: 'POST',
      body: JSON.stringify({ agent_deployment_id: deploymentId, title }),
    }),
  createRun: (threadId: string, input: string) =>
    request<Run>(`/api/v1/threads/${threadId}/runs`, {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ input }),
    }),
  cancelRun: (id: string) => request<Run>(`/api/v1/runs/${id}:cancel`, { method: 'POST' }),
  retryRun: (id: string) => request<Run>(`/api/v1/runs/${id}:retry`, { method: 'POST' }),
  provideRunInput: (id: string, input: string) => request<Run>(`/api/v1/runs/${id}/input`, {
    method: 'POST',
    body: JSON.stringify({ input }),
  }),
  interrupts: (status?: string) =>
    request<{ items: Interrupt[] }>(`/api/v1/interrupts${status ? `?status=${status}` : ''}`),
  decide: (interrupt: Interrupt, type: 'approve' | 'edit' | 'reject' | 'respond', message?: string, editedArguments?: Record<string, unknown>) =>
    request<Interrupt>(`/api/v1/interrupts/${interrupt.id}/decisions`, {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID(), 'If-Match': String(interrupt.version) },
      body: JSON.stringify({
        decisions: [{ action_id: interrupt.actions[0].action_id, type, message, edited_arguments: editedArguments }],
      }),
    }),
}

export function streamUrl(runId: string, afterSequence = 0) {
  return `${API_BASE}/api/v1/runs/${runId}/stream?after_sequence=${afterSequence}&channel=all`
}
