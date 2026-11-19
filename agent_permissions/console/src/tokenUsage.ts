import { z } from "zod";

const count = z.int().nonnegative();

export const tokenUsageSummary = z.object({
  user_id: z.string(),
  from_date: z.iso.date().nullable(),
  to_date: z.iso.date().nullable(),
  timezone: z.literal("Asia/Shanghai"),
  input_tokens: count.nullable(),
  output_tokens: count.nullable(),
  total_tokens: count.nullable(),
  cached_input_tokens: count.nullable(),
  reasoning_tokens: count.nullable(),
  model_calls: count,
  unknown_calls: count,
  legacy_runs: count,
});

export type TokenUsageSummary = z.infer<typeof tokenUsageSummary>;

export interface TokenUsageQuery {
  from_date?: string;
  to_date?: string;
}
