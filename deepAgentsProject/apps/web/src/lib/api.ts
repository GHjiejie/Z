import type {
  ProductionRoutingProfile,
  RoutingChangeDraft,
  RoutingChangeRequest,
  RoutingDeploymentSummary,
  CursorPage,
  EnvironmentGrant,
  ReleaseChannel,
  ReleaseRequest,
  Agent,
  AgentDraft,
  Deployment,
  Interrupt,
  IntentRoutingDecision,
  IntentRoutingProfile,
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
  ThreadAccess,
  Repository,
  LocalRepositoryFolderListing,
  CodingWorkspace,
  WorkspaceTreeItem,
  VerificationReport,
  ChangeSet,
  LoginSession,
  PlatformUser,
  UserListResponse,
  AuthSession,
  AuthAuditListResponse,
} from '../types'
import type { KnowledgeUploadBody } from './knowledgeRetry'
import { uploadHeaders } from './knowledgeRetry'
import { errorWithRequestIdentity, requestIdentity } from './requestIdentity'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

function listQuery(params: { cursor?: string; limit?: number; q?: string; status?: string }) {
  const query = new URLSearchParams(Object.entries(params)
    .filter(([, value]) => value !== undefined && value !== '')
    .map(([key, value]) => [key, String(value)]))
  return query.size ? `?${query}` : ''
}

function csrfToken() {
  const prefix = 'deepagent_csrf='
  const item = document.cookie.split(';').map((value) => value.trim()).find((value) => value.startsWith(prefix))
  return item ? decodeURIComponent(item.slice(prefix.length)) : undefined
}

export class ApiError extends Error {
  status: number
  code?: string
  requestId?: string

  constructor(message: string, status: number, code?: string, requestId?: string | null) {
    super(errorWithRequestIdentity(message, requestId))
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestIdentity(requestId)
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method ?? 'GET').toUpperCase()
  const csrf = UNSAFE_METHODS.has(method) ? csrfToken() : undefined
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    credentials: 'include',
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      ...options.headers,
    },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const error = new ApiError(body?.error?.message ?? body?.detail ?? `Request failed (${response.status})`, response.status, body?.error?.code, response.headers.get('X-Request-ID'))
    if (response.status === 401 && path !== '/api/v1/auth/login' && path !== '/api/v1/auth/me') {
      window.dispatchEvent(new CustomEvent('deepagent:unauthorized'))
    }
    throw error
  }
  return response.json() as Promise<T>
}

export const api = {
  releaseGrants: () => request<{ items: EnvironmentGrant[] }>('/api/v1/deployment-environment-grants'),
  releaseChannel: (agentId: string) => request<ReleaseChannel>(`/api/v1/agents/${agentId}/release-channel`),
  releaseRequests: (params: { cursor?: string; limit?: number } = {}) =>
    request<CursorPage<ReleaseRequest>>(`/api/v1/release-requests${listQuery(params)}`),
  releaseRequest: (id: string) => request<ReleaseRequest>(`/api/v1/release-requests/${id}`),
  createRelease: (body: { agent_revision_id: string; expected_channel_version: number; reason: string;
    action: 'promote' | 'rollback'; rollback_deployment_id?: string }, key: string) =>
    request<ReleaseRequest>('/api/v1/release-requests', { method: 'POST',
      headers: { 'Idempotency-Key': key }, body: JSON.stringify(body) }),
  decideRelease: (item: ReleaseRequest, decision: 'approve' | 'reject', reason: string) =>
    request<ReleaseRequest>(`/api/v1/release-requests/${item.id}:decide`, { method: 'POST',
      body: JSON.stringify({ version: item.version, decision, reason }) }),
  cancelRelease: (item: ReleaseRequest, reason: string) =>
    request<ReleaseRequest>(`/api/v1/release-requests/${item.id}:cancel`, { method: 'POST',
      body: JSON.stringify({ version: item.version, reason }) }),
  login: (username: string, password: string) => request<LoginSession>('/api/v1/auth/login', {
    method: 'POST', body: JSON.stringify({ username, password }),
  }),
  logout: () => request<{ ok: boolean }>('/api/v1/auth/logout', { method: 'POST' }),
  me: () => request<PlatformUser>('/api/v1/auth/me'),
  changePassword: (body: { current_password: string; new_password: string; version: number }) =>
    request<PlatformUser>('/api/v1/auth/password', { method: 'PUT', body: JSON.stringify(body) }),
  ownSessions: () => request<{ items: AuthSession[] }>('/api/v1/auth/sessions'),
  revokeOwnSession: (sessionId: string) => request<{ ok: boolean; revoked_count: number; revoked_current: boolean }>(`/api/v1/auth/sessions/${sessionId}`, { method: 'DELETE' }),
  revokeAllOwnSessions: () => request<{ ok: boolean; revoked_count: number; revoked_current: boolean }>('/api/v1/auth/sessions', { method: 'DELETE' }),
  users: (params: {
    page?: number
    page_size?: number
    q?: string
    status?: 'ACTIVE' | 'INACTIVE' | 'ALL'
    role?: string
    tenant_id?: string
    project_id?: string
    sort_by?: 'username' | 'display_name' | 'status' | 'created_at' | 'updated_at' | 'last_login_at'
    sort_order?: 'asc' | 'desc'
  } = {}) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== '').map(([key, value]) => [key, String(value)]))
    return request<UserListResponse>(`/api/v1/users${query.size ? `?${query}` : ''}`)
  },
  createUser: (body: {
    username: string
    display_name: string
    password: string
    tenant_id: string
    project_id: string
    environment_id: string
    roles: string[]
    is_super_admin: boolean
  }) => request<PlatformUser>('/api/v1/users', { method: 'POST', body: JSON.stringify(body) }),
  updateUser: (id: string, body: Partial<Pick<PlatformUser, 'username' | 'display_name' | 'tenant_id' | 'project_id' | 'environment_id' | 'roles' | 'is_super_admin' | 'status'>> & { version: number }) =>
    request<PlatformUser>(`/api/v1/users/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  resetUserPassword: (id: string, password: string, version: number) =>
    request<PlatformUser>(`/api/v1/users/${id}/password`, { method: 'PUT', body: JSON.stringify({ password, version }) }),
  deactivateUser: (id: string, version: number, reason: string) => request<PlatformUser>(`/api/v1/users/${id}`, { method: 'DELETE', body: JSON.stringify({ version, reason }) }),
  userSessions: (id: string) => request<{ items: AuthSession[] }>(`/api/v1/users/${id}/sessions`),
  revokeUserSession: (id: string, sessionId: string) => request<{ ok: boolean; revoked_count: number; revoked_current: boolean }>(`/api/v1/users/${id}/sessions/${sessionId}`, { method: 'DELETE' }),
  revokeAllUserSessions: (id: string) => request<{ ok: boolean; revoked_count: number; revoked_current: boolean }>(`/api/v1/users/${id}/sessions`, { method: 'DELETE' }),
  userAuditEvents: (params: { page?: number; page_size?: number; q?: string; action?: string; outcome?: string; target_user_id?: string } = {}) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, value]) => value !== undefined && value !== '').map(([key, value]) => [key, String(value)]))
    return request<AuthAuditListResponse>(`/api/v1/users/audit-events${query.size ? `?${query}` : ''}`)
  },
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
  routingProfile: () => request<IntentRoutingProfile>('/api/v1/intent-routing/profile'),
  productionRouting: () => request<{ profile: ProductionRoutingProfile; deployments: RoutingDeploymentSummary[] }>('/api/v1/production-routing/profile'),
  routingHistory: (cursor?: string) => request<CursorPage<ProductionRoutingProfile>>('/api/v1/production-routing/revisions' + listQuery({ cursor })),
  routingChanges: (cursor?: string) => request<CursorPage<RoutingChangeRequest>>('/api/v1/routing-change-requests' + listQuery({ cursor })),
  routingChange: (id: string) => request<RoutingChangeRequest>(`/api/v1/routing-change-requests/${id}`),
  createRoutingChange: (body: {
    expected_router_revision_id: string; action: 'update' | 'rollback'; reason: string;
    profile?: RoutingChangeDraft; rollback_revision_id?: string
  }, key: string) => request<RoutingChangeRequest>('/api/v1/routing-change-requests', {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify(body),
  }),
  decideRoutingChange: (item: RoutingChangeRequest, action: 'approve' | 'reject' | 'cancel', reason: string) =>
    request<RoutingChangeRequest>(`/api/v1/routing-change-requests/${item.id}:${action === 'cancel' ? 'cancel' : 'decide'}`, {
      method: 'POST', body: JSON.stringify({ version: item.version, reason, ...(action === 'cancel' ? {} : { decision: action }) }),
    }),
  updateRoutingProfile: (body: {
    mode: IntentRoutingProfile['mode']
    auto_route_threshold: number
    confirmation_threshold: number
    decision_ttl_seconds: number
    target_deployments: Partial<IntentRoutingProfile['config']['target_deployments']>
  }) => request<IntentRoutingProfile>('/api/v1/intent-routing/profile', {
    method: 'PUT', body: JSON.stringify(body),
  }),
  resolveIntentRoute: (body: {
    input: string
    preferred_deployment_id?: string
    workspace?: { repository_id: string; base_ref?: string; source_mode?: 'committed_ref' | 'working_tree_snapshot' }
  }) => request<IntentRoutingDecision>('/api/v1/intent-routing:resolve', {
    method: 'POST', body: JSON.stringify(body),
  }),
  createRoutedRun: (body: {
    decision_id: string
    input: string
    title?: string
    confirmed?: boolean
    override_deployment_id?: string
    workspace?: { repository_id: string; base_ref?: string; source_mode?: 'committed_ref' | 'working_tree_snapshot' }
  }) => request<{ decision: IntentRoutingDecision; thread: ThreadSummary; run: Run }>('/api/v1/routed-runs', {
    method: 'POST', body: JSON.stringify(body),
  }),
  routingDecisions: () => request<{ items: IntentRoutingDecision[] }>('/api/v1/intent-routing/decisions'),
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
  createKnowledgeBase: (body: { name: string; description: string }, idempotencyKey: string) =>
    request<KnowledgeBase>('/api/v1/knowledge-bases', {
      method: 'POST', body: JSON.stringify(body), headers: { 'Idempotency-Key': idempotencyKey },
    }),
  knowledgeRevisions: (knowledgeBaseId: string) =>
    request<{ items: KnowledgeRevision[] }>(`/api/v1/knowledge-bases/${knowledgeBaseId}/revisions`),
  prepareKnowledgeUpload: (knowledgeBaseId: string, body: KnowledgeUploadBody, idempotencyKey: string) =>
    request<KnowledgeUploadPreparation>(`/api/v1/knowledge-bases/${knowledgeBaseId}/documents:prepare-upload`, {
      method: 'POST',
      headers: { 'Idempotency-Key': idempotencyKey },
      body: JSON.stringify(body),
    }),
  uploadKnowledgeFile: async (preparation: KnowledgeUploadPreparation, file: File) => {
    if (!preparation.upload) return { etag: undefined }
    const platformUpload = preparation.upload.url.startsWith('/')
    const response = await fetch(platformUpload ? `${API_BASE}${preparation.upload.url}` : preparation.upload.url, {
      method: preparation.upload.method,
      credentials: platformUpload ? 'include' : 'omit',
      body: file,
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        ...(platformUpload && csrfToken() ? { 'X-CSRF-Token': csrfToken()! } : {}),
        ...uploadHeaders(preparation.upload.required_headers, file.size),
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
  threads: (params: { cursor?: string; limit?: number; q?: string } = {}) => request<CursorPage<ThreadSummary>>(`/api/v1/threads${listQuery(params)}`),
  thread: (id: string) => request<ThreadSummary>(`/api/v1/threads/${id}`),
  threadAccess: (id: string) => request<ThreadAccess>(`/api/v1/threads/${id}/access`),
  sharingCandidates: (id: string, query: string) => request<{ items: Array<{ id: string; username: string; display_name: string }> }>(`/api/v1/threads/${id}/sharing-candidates?q=${encodeURIComponent(query)}`),
  updateThreadAccess: (id: string, body: Pick<ThreadAccess, 'version' | 'visibility' | 'members'> & { reason: string }) => request<ThreadAccess>(`/api/v1/threads/${id}/access`, { method: 'PUT', body: JSON.stringify(body) }),
  runs: (params: { cursor?: string; limit?: number; q?: string; status?: string } = {}) => request<CursorPage<Run>>(`/api/v1/runs${listQuery(params)}`),
  run: (id: string) => request<Run>(`/api/v1/runs/${id}`),
  runEvents: (id: string, after = 0) =>
    request<{ items: RuntimeEvent[]; next_sequence: number; has_more: boolean }>(`/api/v1/runs/${id}/events?after_sequence=${after}`),
  runArtifacts: (id: string) => request<{ items: RunArtifact[] }>(`/api/v1/runs/${id}/artifacts`),
  runSpans: (id: string) => request<{ items: Array<Record<string, unknown>> }>(`/api/v1/runs/${id}/spans`),
  createThread: (deploymentId: string, title: string) =>
    request<ThreadSummary>('/api/v1/threads', {
      method: 'POST',
      body: JSON.stringify({ agent_deployment_id: deploymentId, title }),
    }),
  repositories: () => request<{ items: Repository[] }>('/api/v1/repositories'),
  localRepositoryFolders: (path?: string) =>
    request<LocalRepositoryFolderListing>(`/api/v1/local-repository-folders${path ? `?path=${encodeURIComponent(path)}` : ''}`),
  createRepository: (body: { name: string; provider: Repository['provider']; canonical_uri: string; default_branch: string }) =>
    request<Repository>('/api/v1/repositories', { method: 'POST', body: JSON.stringify(body) }),
  probeRepository: (id: string) => request<Record<string, unknown>>(`/api/v1/repositories/${id}:probe`, { method: 'POST' }),
  createCodingThread: (deploymentId: string, title: string, repositoryId: string, baseRef: string, sourceMode: 'committed_ref' | 'working_tree_snapshot' = 'committed_ref') =>
    request<ThreadSummary>('/api/v1/threads', {
      method: 'POST',
      body: JSON.stringify({
        agent_deployment_id: deploymentId,
        title,
        workspace: { repository_id: repositoryId, base_ref: baseRef, source_mode: sourceMode },
      }),
    }),
  threadWorkspace: (threadId: string) => request<CodingWorkspace>(`/api/v1/threads/${threadId}/workspace`),
  workspaceTree: (runId: string) => request<{ workspace_id: string; workspace_generation: number; items: WorkspaceTreeItem[]; truncated: boolean }>(`/api/v1/runs/${runId}/workspace/tree`),
  workspaceFile: (runId: string, path: string) => request<{ path: string; content: string; encoding: string; size_bytes: number; workspace_generation: number }>(`/api/v1/runs/${runId}/workspace/file?path=${encodeURIComponent(path)}`),
  runDiff: (runId: string) => request<ChangeSet | { run_id: string; status: 'PENDING'; patch: string; changed_files: [] }>(`/api/v1/runs/${runId}/diff`),
  runVerification: (runId: string) => request<VerificationReport>(`/api/v1/runs/${runId}/verification`),
  runChangeSets: (runId: string) => request<{ items: ChangeSet[] }>(`/api/v1/runs/${runId}/changesets`),
  decideChangeSet: (runId: string, changeSetId: string, approve: boolean, version: number, message?: string) =>
    request<ChangeSet>(`/api/v1/runs/${runId}/changesets/${changeSetId}:${approve ? 'approve' : 'reject'}`, {
      method: 'POST', body: JSON.stringify({ message, version }),
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
        decisions: interrupt.actions.map((action, index) => ({
          action_id: action.action_id,
          type: type === 'edit' && index > 0 ? 'approve' : type,
          message,
          edited_arguments: type === 'edit' && index === 0 ? editedArguments : undefined,
        })),
      }),
    }),
}

export function streamUrl(runId: string, afterSequence = 0) {
  return `${API_BASE}/api/v1/runs/${runId}/stream?after_sequence=${afterSequence}&channel=all`
}
