export type Json = Record<string, unknown>

export interface PlatformContext {
  user: { id: string; name: string; role: string }
  tenant: { id: string; name: string }
  project: { id: string; name: string }
  environment: { id: string; name: string }
  runtime: {
    status: string
    workers_online: number
    workers_total: number
    queue_depth: number
    event_lag_ms: number | null
    updated_at: string
  }
  features: {
    global_search: boolean
    notifications: boolean
    workspace_switching: boolean
    environment_switching: boolean
    resource_registration: boolean
    routing_management: boolean
    attachments: boolean
    code_context: boolean
  }
}

export interface AgentDraft {
  harness_type: 'deepagents' | 'langchain_agent' | 'custom_langgraph'
  harness_profile_revision_id: string
  model_deployment_id: string
  system_prompt: string
  capabilities: {
    tools: string[]
    mcp_servers: string[]
    skills: string[]
    memories: string[]
    knowledge_bases: string[]
    subagents: string[]
    filesystem: boolean
  }
  policies: {
    permission_policy: string
    approval_mode: 'never' | 'high_risk' | 'always'
    audit_level: 'standard' | 'strict'
  }
  limits: {
    max_duration_seconds: number
    max_model_calls: number
    max_tool_calls: number
    max_subagent_depth: number
    max_subagent_concurrency: number
    max_sandbox_cpu_seconds: number
    max_output_bytes: number
    max_cost: number | null
  }
  output_schema?: Json | null
  coding?: {
    enabled: boolean
    sandbox: {
      revision_id?: string
      provider: 'docker' | 'kubernetes' | 'fake'
      image: string
      image_digest: string
      user: string
      cpu_limit: number
      memory_mb: number
      disk_mb: number
      pids_limit: number
      command_timeout_seconds: number
      run_timeout_seconds: number
      max_output_bytes: number
      network_mode: 'deny_by_default' | 'allowlist'
      workspace_root: string
      read_only_rootfs: boolean
      lifecycle: 'run_scoped' | 'thread_scoped' | 'agent_scoped'
      ttl_seconds: number
    }
    repository_policy_revision_id?: string
    delivery_mode: 'patch_only' | 'commit' | 'pull_request'
    verification_policy: {
      auto_discover: boolean
      required_commands: string[]
      max_attempts: number
      command_timeout_seconds: number
      require_success: boolean
    }
    protected_paths: string[]
    max_changed_files: number
    max_diff_lines: number
  } | null
}

export interface Deployment {
  id: string
  name: string
  agent_name?: string
  agent_id: string
  agent_revision_id: string
  resolved_plan_id: string
  environment: string
  status: string
  coding_enabled?: boolean
  knowledge_enabled?: boolean
  coding_profile?: AgentDraft['coding']
  created_at: string
}

export interface Agent {
  id: string
  name: string
  description: string
  draft: AgentDraft
  status: string
  version: number
  revision_count: number
  latest_deployment?: Deployment | null
  revisions?: Revision[]
  deployments?: Deployment[]
  created_at: string
  updated_at: string
}

export interface Revision {
  id: string
  agent_id: string
  revision_number: number
  spec: AgentDraft
  created_at: string
  resolved_plan?: ResolvedPlan
}

export interface ResolvedPlan {
  id: string
  plan_hash: string
  runtime_image_digest: string
  plan: Json
}

export interface Usage {
  input_tokens: number
  output_tokens: number
  model_calls: number
  tool_calls: number
  subagent_calls: number
  cost: number
}

export interface Run {
  id: string
  thread_id: string
  thread_title?: string
  agent_name?: string
  environment?: string
  status: string
  input: string
  output?: string | null
  resolved_plan_id: string
  current_attempt_id: string
  routing_decision_id?: string | null
  attempt_count?: number
  metadata: Json
  checkpoint: Json
  attempts?: Array<Record<string, unknown>>
  usage?: Usage
  created_at: string
  updated_at: string
}

export interface ThreadSummary {
  id: string
  agent_deployment_id: string
  title: string
  deployment_name?: string
  agent_name?: string
  last_run?: { id: string; status: string; updated_at: string } | null
  runs?: Run[]
  repository_id?: string | null
  repository_snapshot_id?: string | null
  routing_decision_id?: string | null
  workspace?: CodingWorkspace | null
  created_at: string
  updated_at: string
}

export type PrimaryIntent = 'coding' | 'release' | 'knowledge' | 'general' | 'ambiguous'

export interface IntentClassification {
  taxonomy_version: string
  primary_intent: PrimaryIntent
  secondary_intents: PrimaryIntent[]
  subtype: string
  confidence: number
  requires_repository: boolean
  requires_knowledge: boolean
  risk_hint: 'low' | 'medium' | 'high'
  summary: string
  source: 'rules' | 'model' | 'fallback'
}

export interface RoutingDeploymentSummary {
  id: string
  name: string
  agent_name: string
  environment: string
  coding_enabled: boolean
  knowledge_enabled: boolean
}

export interface IntentRoutingDecision {
  id: string
  router_revision_id: string
  input_hash: string
  status: 'READY' | 'NEEDS_WORKSPACE' | 'NEEDS_CONFIRMATION' | 'FALLBACK'
  classification: IntentClassification
  selected_deployment_id?: string | null
  predicted_deployment_id?: string | null
  selected_deployment?: RoutingDeploymentSummary | null
  predicted_deployment?: RoutingDeploymentSummary | null
  candidate_deployments: RoutingDeploymentSummary[]
  requirements: { workspace: boolean; confirmation: boolean; low_confidence: boolean }
  reason: string
  thread_id?: string | null
  run_id?: string | null
  committed: boolean
  expires_at: string
  created_at: string
  committed_at?: string | null
}

export interface IntentRoutingProfile {
  id: string
  revision_number: number
  taxonomy_version: string
  mode: 'active' | 'shadow' | 'disabled'
  status: string
  config: {
    auto_route_threshold: number
    confirmation_threshold: number
    decision_ttl_seconds: number
    target_deployments: Record<'coding' | 'release' | 'knowledge' | 'general', string | null>
  }
  target_details: Record<'coding' | 'release' | 'knowledge' | 'general', RoutingDeploymentSummary | null>
  created_at: string
}

export interface Repository {
  id: string
  name: string
  provider: 'local_snapshot' | 'generic_git' | 'github' | 'gitlab'
  canonical_uri: string
  default_branch: string
  status: string
  snapshot_count?: number
  created_at: string
  updated_at: string
}

export interface LocalRepositoryFolder {
  name: string
  path: string
  is_git_repository: boolean
  default_branch?: string | null
}

export interface LocalRepositoryFolderListing {
  roots: string[]
  current_path: string
  parent_path?: string | null
  current: LocalRepositoryFolder
  items: LocalRepositoryFolder[]
  truncated: boolean
}

export interface CodingWorkspace {
  id: string
  thread_id: string
  repository_id?: string
  repository_name?: string
  repository_snapshot_id: string
  resolved_commit_sha?: string
  requested_ref?: string
  workspace_generation: number
  status: string
  sandbox?: { id: string; provider: string; status: string; provider_metadata?: Json } | null
  expires_at: string
}

export interface WorkspaceTreeItem { path: string; name: string; type: string }

export interface VerificationReport {
  id?: string
  run_id: string
  status: string
  checks: Array<{ id: string; command: string; exit_code: number; status: string; output_preview?: string }>
  summary: { total?: number; passed?: number; failed?: number; require_success?: boolean }
}

export interface ChangeSet {
  id: string
  run_id: string
  base_commit_sha: string
  workspace_generation: number
  diff_stat: { files: number; added: number; deleted: number }
  changed_files: Array<{ path: string; status: string; original_path?: string; sha256?: string | null }>
  status: string
  content_hash: string
  plan_hash: string
  patch?: string
  created_at: string
}

export interface RunArtifact {
  id: string
  run_id: string
  name: string
  media_type: string
  uri: string
  content_hash: string
  size_bytes: number
  created_at: string
}

export interface RuntimeEvent {
  event_id: string
  sequence: number
  type: string
  occurred_at: string
  span_id?: string | null
  parent_span_id?: string | null
  execution_path: string[]
  payload: Record<string, any>
}

export interface InterruptAction {
  action_id: string
  tool_name: string
  arguments: Record<string, unknown>
  risk_level: string
  allowed_decisions: string[]
}

export interface Interrupt {
  id: string
  run_id: string
  run_input?: string
  agent_name?: string
  checkpoint_id: string
  policy_reason: string
  status: string
  version: number
  actions: InterruptAction[]
  decision?: Record<string, unknown> | null
  expires_at: string
  created_at: string
  updated_at?: string
}

export interface ModelDeployment {
  id: string
  name: string
  provider: string
  model: string
  endpoint_region: string
  status: string
  capabilities: string[]
  pricing: { input_per_million: number; output_per_million: number }
}

export interface Plugin {
  id: string
  name: string
  version: string
  description: string
  manifest_hash: string
  status: string
  skill_count: number
  loaded_at: string
}

export interface Skill {
  id: string
  plugin_id: string
  plugin_name: string
  slug: string
  name: string
  description: string
  current_version_id: string
  version: string
  artifact_hash: string
  tags: string[]
  status: string
  builtin: number
}

export interface Overview {
  agents: number
  deployments: number
  pending_approvals: number
  run_statuses: Record<string, number>
  success_rate: number | null
  usage: Usage & { tokens: number }
  recent_runs: Run[]
  runtime: { workers: number; queue_depth: number; event_lag_ms: number | null; status: string; updated_at: string }
}

export interface KnowledgeDocument {
  id: string
  knowledge_base_id: string
  display_name: string
  description: string
  source_type: string
  status: string
  visibility: 'private' | 'project'
  allowed_roles: string[]
  current_version_id?: string | null
  content_type?: string | null
  size_bytes?: number | null
  content_sha256?: string | null
  canonical_uri?: string | null
  version_status?: string | null
  indexed_at?: string | null
  created_at: string
  updated_at: string
}

export interface KnowledgeRevision {
  id: string
  knowledge_base_id: string
  revision_number: number
  status: string
  manifest: Record<string, unknown>
  retrieval_profile: Record<string, unknown>
  embedding_model: string
  embedding_dimensions: number
  index_hash: string
  created_at: string
  activated_at?: string | null
  deprecated_at?: string | null
}

export interface KnowledgeBase {
  id: string
  name: string
  description: string
  status: string
  current_revision_id?: string | null
  document_count?: number
  ready_document_count?: number
  documents?: KnowledgeDocument[]
  revisions?: KnowledgeRevision[]
  created_at: string
  updated_at: string
}

export interface KnowledgeUploadPreparation {
  document_id: string
  document_version_id: string
  storage: {
    provider: string
    bucket: string
    region: string
    canonical_uri: string
  }
  upload: {
    method: string
    url: string
    expires_at: string
    required_headers: Record<string, string>
  }
}

export interface KnowledgeIngestionJob {
  id: string
  knowledge_base_id: string
  document_version_id: string
  status: string
  stage: string
  attempts: number
  chunk_count?: number | null
  error_code?: string | null
  error_message?: string | null
  created_at: string
  updated_at: string
}

export interface KnowledgeSearchHit {
  citation_id: string
  chunk_id: string
  document_id: string
  document_version_id: string
  text: string
  score: number
  source: {
    title: string
    content_type: string
    locator: Record<string, unknown>
    page?: number | null
    section?: string | null
    content_hash: string
    canonical_uri: string
    download_url: string
  }
}

export interface KnowledgeSearchResult {
  status: string
  hits: KnowledgeSearchHit[]
  revision_ids: string[]
  latency_ms: number
}
