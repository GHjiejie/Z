import { z } from "zod";

const stamp = z.iso.datetime({ offset: true });
const text = z.string().optional();
const count = z.number().int().nonnegative().optional();

export const auditOutcome = z.enum([
  "not_executed",
  "succeeded",
  "failed",
  "unknown",
]);
export type AuditOutcome = z.infer<typeof auditOutcome>;

const auditOperation = z.object({
  call_id: z.string(),
  started_at: stamp,
  finished_at: stamp.optional(),
  actor_user_id: text,
  subject_user_id: text,
  entrypoint: text,
  agent_id: text,
  run_id: text,
  execution_id: text,
  control_request_id: text,
  business_request_id: text,
  operation_id: text,
  http_method: text,
  route_template: text,
  requested_target_id: text,
  target_id: text,
  confirmed_by_user_id: text,
  confirmed_at: stamp.optional(),
  status: z.string(),
  dispatch_state: z.enum(["not_sent", "attempted", "received"]),
  business_outcome: auditOutcome,
  reason_code: text,
  duration_ms: count,
  control_http_status: count,
  business_http_status: count,
  result_summary: z
    .object({ page: count, page_size: count, returned_count: count })
    .optional(),
  change_summary: z
    .object({
      fields: z.array(z.string()).optional(),
      no_change: z.boolean().optional(),
      password_changed: z.boolean().optional(),
    })
    .optional(),
});
export type AuditOperation = z.infer<typeof auditOperation>;

const permissionCheck = z.object({
  stage: z.string(),
  check: z.string(),
  decision: z.enum(["allowed", "denied", "not_evaluated", "not_applicable"]),
  roles: z.array(z.string()).optional(),
  required_all: z.array(z.string()).optional(),
  required_any: z.array(z.string()).optional(),
  matched: z.array(z.string()).optional(),
  missing: z.array(z.string()).optional(),
  grant_enabled: z.boolean().optional(),
  grant_changed_by_user_id: text,
  grant_changed_at: stamp.optional(),
  grant_version: count,
  event_id: text,
});
export type AuditPermissionCheck = z.infer<typeof permissionCheck>;

export const auditOperationDetail = auditOperation.extend({
  permission_checks: z.array(permissionCheck),
  events: z.array(
    z.object({
      event_id: z.string(),
      event_type: z.string(),
      occurred_at: stamp,
      reason_code: text,
      attempt_id: text,
    }),
  ),
  children: z.array(auditOperation),
});
export type AuditOperationDetail = z.infer<typeof auditOperationDetail>;

const grantChange = z.object({
  event_id: z.string(),
  occurred_at: stamp,
  actor_user_id: z.string(),
  subject_user_id: z.string(),
  operation_id: z.string(),
  grant_version: z.number().int().positive(),
  change_summary: z.object({ before: z.boolean(), after: z.boolean() }),
});
export type AuditGrantChange = z.infer<typeof grantChange>;

export const auditOperationPage = z.object({
  items: z.array(auditOperation),
  next_cursor: z.string().nullable(),
});
export const auditGrantPage = z.object({
  items: z.array(grantChange),
  next_cursor: z.string().nullable(),
});

export interface AuditQuery {
  operation_id?: string;
  actor_user_id?: string;
  subject_user_id?: string;
  cursor?: string;
  limit: number;
}

export function auditQueryString(
  query: AuditQuery & { business_outcome?: AuditOutcome },
): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== "") params.set(key, String(value));
  }
  return params.toString();
}
