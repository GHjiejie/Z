export interface StreamEvent {
  type: string;
  protocol_version?: number;
  run_id?: string;
  sequence?: number;
  timestamp?: number;
  [key: string]: unknown;
}

export type RunStatus =
  | "connecting"
  | "running"
  | "completed"
  | "interrupted"
  | "failed";

interface TimelineItemBase {
  id: string;
  sequence: number;
  startedAt: number;
  completedAt?: number;
}

export interface ThoughtItem extends TimelineItemBase {
  kind: "thought";
  content: string;
}

export interface AnswerItem extends TimelineItemBase {
  kind: "answer";
  content: string;
}

export interface ToolItem extends TimelineItemBase {
  kind: "tool";
  name: string;
  status: "preparing" | "running" | "completed" | "failed";
  input: string;
  output: string;
}

export type TimelineItem = ThoughtItem | AnswerItem | ToolItem;

export interface RunView {
  id: string;
  prompt: string;
  status: RunStatus;
  startedAt: number;
  durationMs?: number;
  items: TimelineItem[];
  stateUpdates: number;
  error?: string;
}
