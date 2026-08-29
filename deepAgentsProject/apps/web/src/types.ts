export type Json = Record<string, unknown>

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
  attempt_count?: number
  metadata: Json
  checkpoint: Json
  attempts?: Array<Record<string, unknown>>
  usage?: Usage
  created_at: string
  updated_at: string
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
  success_rate: number
  usage: Usage & { tokens: number }
  recent_runs: Run[]
  runtime: { workers: number; queue_depth: number; event_lag_ms: number; status: string }
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
