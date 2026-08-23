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

