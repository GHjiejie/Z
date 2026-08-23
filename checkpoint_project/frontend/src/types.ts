export type MessageContent = string | Array<Record<string, unknown>>;

export interface ToolCall {
  id: string;
  name: string;
  args: Record<string, unknown>;
  type?: string;
}

export interface ArtifactRef {
  artifact_id: string;
  kind: "html" | string;
  mime_type: string;
  title: string;
  byte_size: number;
  parent_artifact_id: string | null;
  created_at: string;
  content_url: string;
}

export interface Artifact extends ArtifactRef {
  content: string;
  content_sha256: string;
}

export interface ChatMessage {
  id: string | null;
  type: "human" | "ai" | "tool" | "system" | string;
  content: MessageContent;
  tool_calls?: ToolCall[];
  tool_call_id?: string;
  name?: string;
  tool_status?: "success" | "error";
  artifact?: ArtifactRef;
}

export interface ApprovalPayload {
  kind: string;
  tool: "write_file" | "delete_file" | string;
  path?: string;
  resolved_path?: string;
  title?: string;
  overwrite?: boolean;
  characters?: number;
  byte_size?: number;
  preview?: string;
  tool_call_id?: string;
}

export interface PendingApproval {
  id: string | null;
  payload: ApprovalPayload;
}

export interface SessionState {
  thread_id: string;
  checkpoint_id: string | null;
  messages: ChatMessage[];
  turn_count: number;
  next: string[];
  status: "idle" | "waiting_approval" | "recoverable";
  pending_approvals: PendingApproval[];
}

export interface SessionSummary {
  thread_id: string;
  created_at: string;
  source_thread_id: string | null;
  source_checkpoint_id: string | null;
  message_count: number;
  status: SessionState["status"];
  last_message: ChatMessage | null;
}

export interface Checkpoint {
  index: number;
  checkpoint_id: string;
  created_at: string;
  next: string[];
  message_count: number;
  turn_count: number;
  last_message: ChatMessage | null;
  has_interrupt: boolean;
  step: number | null;
  source: string | null;
}
