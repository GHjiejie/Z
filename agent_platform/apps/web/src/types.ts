export type User = {
  id: string;
  email: string;
  name: string;
  role: "admin" | "member" | "finance_viewer";
  tenant_role?: TenantRole;
  membership_version?: number;
  tenant_status?: string;
  capabilities?: string[];
  platform_roles?: string[];
  tenant_id: string;
  active: boolean;
};
export type Model = {
  id: string;
  name: string;
  alias: string;
  is_default?: boolean;
  active: boolean;
  input_price: string;
  output_price: string;
  context_window: number;
  max_output_tokens: number;
};
export type GatewayAdmin = {
  admin_url: string;
  configured: boolean;
};
export type Agent = {
  id: string;
  name: string;
  description: string;
  system_prompt: string;
  model_id: string | null;
  builtin_key?: string | null;
  category?: string;
  starter_prompts?: string[];
  temperature: number;
  max_steps: number;
  max_tokens: number;
  tools: string[];
  published_version: number | null;
  created_at: string;
};
export type Session = {
  id: string;
  title: string;
  agent_id: string;
  created_at: string;
};
export type Run = {
  id: string;
  session_id: string;
  agent_name?: string;
  status: string;
  created_at: string;
  error?: string;
  cost?: string;
  model_id?: string;
  model_alias?: string;
};
export type Message = { role: string; content: string; created_at?: string };
export type SessionDetail = Session & { messages: Message[]; runs: Run[] };
export type RunEvent = {
  sequence: number;
  type: string;
  data: Record<string, unknown>;
};
export type Wallet = {
  balance: string;
  reserved: string;
  available: string;
  currency: string;
  blocked?: boolean;
  block_reason?: string;
};
export type Dashboard = Wallet & {
  requests: number;
  tokens: number;
  cost: string;
  success_rate: number;
  active_runs: number;
  daily: { date: string; requests: number; tokens: number; cost: string }[];
  models: { model: string; requests: number; tokens: number; cost: string }[];
  gateway_configured: boolean;
};
export type Usage = {
  id: string;
  model: string;
  agent_name: string;
  user_email: string;
  status: string;
  input_tokens: number;
  output_tokens: number;
  cost: string;
  provider_cost: string | null;
  created_at: string;
};
export type Ledger = {
  id: string;
  type: string;
  amount: string;
  balance: string;
  description: string;
  created_at: string;
};
export type Quota = {
  id: string;
  scope: string;
  subject_id: string;
  rpm: number | null;
  tpm: number | null;
  concurrent: number | null;
  max_budget: string | null;
  version?: number;
};
export type Audit = {
  id: string;
  actor_email: string;
  action: string;
  target: string;
  created_at: string;
  details?: unknown;
};

export type TenantRole = "owner" | "tenant_admin" | "member" | "finance_viewer";
export type Membership = {
  id: string; user_id: string; tenant_id: string; tenant_name?: string;
  name?: string; email?: string; role: TenantRole; status: string;
  tenant_status?: string; version?: number; authz_version?: number; joined_at?: string;
};
export type IdentityUser = {
  id: string; email: string; name: string; active: boolean;
  platform_roles: string[]; capabilities: string[]; memberships: Membership[];
};
export type Tenant = { id: string; name: string; status: string; version?: number; authz_version?: number; created_at?: string; };
export type Invitation = { id: string; email: string; role: TenantRole; status?: string; expires_at?: string | number; accepted_at?: string | null; revoked_at?: string | null; token?: string; accept_url?: string; };
export type Entitlements = {
  rpm: number | null; tpm: number | null; concurrent: number | null;
  max_queued_runs: number; max_running_runs?: number; max_concurrent_runs?: number;
  max_members: number; max_agents: number; max_sse_connections: number; max_export_jobs: number;
  max_budget: string | null;
  version?: number;
};
