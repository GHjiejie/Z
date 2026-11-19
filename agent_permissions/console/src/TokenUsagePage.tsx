import { useEffect, useState, type FormEvent } from "react";
import { Search } from "lucide-react";
import { aborted, type ApiClient } from "./api";
import { EmptyState, ErrorBanner, Field, Spinner } from "./components";
import type { TokenUsageQuery, TokenUsageSummary } from "./tokenUsage";

type UsageRequest = TokenUsageQuery & { userId: string };
type UsageResponse = { request: UsageRequest } & (
  | { kind: "success"; summary: TokenUsageSummary }
  | { kind: "error"; error: unknown }
);

export function TokenUsagePage({
  api,
  userId,
  canReadOthers,
}: {
  api: ApiClient;
  userId: string;
  canReadOthers: boolean;
}) {
  const [request, setRequest] = useState<UsageRequest>({ userId });
  const [response, setResponse] = useState<UsageResponse | null>(null);
  const [dateError, setDateError] = useState<string | undefined>();
  const resolved = response?.request === request ? response : null;

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    void api
      .tokenUsage(request.userId, request, signal)
      .then((summary) => {
        if (!signal.aborted) setResponse({ request, kind: "success", summary });
      })
      .catch((error: unknown) => {
        if (!signal.aborted && !aborted(error)) {
          setResponse({ request, kind: "error", error });
        }
      });
    return () => controller.abort();
  }, [api, request]);

  function filter(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const from = String(fields.get("from_date") ?? "");
    const to = String(fields.get("to_date") ?? "");
    if (from && to && from > to) {
      setDateError("结束日期不能早于开始日期");
      return;
    }
    setDateError(undefined);
    setRequest({
      userId: canReadOthers
        ? String(fields.get("user_id") ?? "").trim()
        : userId,
      from_date: from || undefined,
      to_date: to || undefined,
    });
  }

  return (
    <>
      <header className="page-heading">
        <div>
          <h1>模型用量</h1>
          <p>
            查看{canReadOthers ? "指定账号" : "当前账号"}的输入、输出及累计
            Token
          </p>
        </div>
      </header>
      <section className="panel usage-query" aria-label="用量查询条件">
        <form
          className={`usage-filters${canReadOthers ? " usage-filters-account" : ""}`}
          onSubmit={filter}
        >
          {canReadOthers && (
            <Field label="用户 ID">
              <input
                name="user_id"
                defaultValue={userId}
                autoComplete="off"
                maxLength={64}
                pattern={"[A-Za-z0-9_\\-]+"}
                required
              />
            </Field>
          )}
          <Field label="开始日期">
            <input
              name="from_date"
              type="date"
              onChange={() => setDateError(undefined)}
            />
          </Field>
          <Field label="结束日期" error={dateError}>
            <input
              name="to_date"
              type="date"
              aria-invalid={dateError ? true : undefined}
              onChange={() => setDateError(undefined)}
            />
          </Field>
          <button type="submit" className="button primary" disabled={!resolved}>
            <Search size={16} aria-hidden="true" /> 查询
          </button>
        </form>
        <p className="usage-note">
          日期按北京时间统计，两项都留空时查看累计用量。
        </p>
      </section>
      <section
        className="usage-results"
        aria-label="用量统计结果"
        aria-busy={!resolved}
      >
        {!resolved && (
          <div className="panel table-loading">
            <Spinner label="正在加载模型用量" />
          </div>
        )}
        {resolved?.kind === "error" && (
          <ErrorBanner
            error={resolved.error}
            onRetry={() => setRequest({ ...request })}
          />
        )}
        {resolved?.kind === "success" && (
          <UsageResults summary={resolved.summary} />
        )}
      </section>
    </>
  );
}

const formatCount = new Intl.NumberFormat("zh-CN").format;

function UsageResults({ summary }: { summary: TokenUsageSummary }) {
  const { from_date: from, to_date: to } = summary;
  const range =
    from && to
      ? `${from} 至 ${to}`
      : from
        ? `${from} 起`
        : to
          ? `截至 ${to}`
          : "累计";
  const metrics = [
    { label: "已确认总 Token", value: summary.total_tokens },
    {
      label: "输入 Token",
      value: summary.input_tokens,
      detail:
        summary.cached_input_tokens === null
          ? null
          : `已报告缓存命中 ${formatCount(summary.cached_input_tokens)}`,
    },
    {
      label: "输出 Token",
      value: summary.output_tokens,
      detail:
        summary.reasoning_tokens === null
          ? null
          : `已报告推理 ${formatCount(summary.reasoning_tokens)}`,
    },
  ];
  return (
    <>
      <header className="usage-results-heading">
        <div>
          <h2>用量概览</h2>
          <p>
            用户 ID <code>{summary.user_id}</code>
          </p>
        </div>
        <span className="usage-period">{range}</span>
      </header>
      {summary.model_calls === 0 && summary.legacy_runs === 0 ? (
        <div className="panel">
          <EmptyState
            title="暂无用量记录"
            description="当前账号在所选日期范围内没有可统计的记录。"
          />
        </div>
      ) : (
        <>
          <dl className="stats-grid usage-stats">
            {metrics.map((metric) => (
              <div className="stat-card" key={metric.label}>
                <dt className="stat-label">{metric.label}</dt>
                <dd>
                  <strong>
                    {metric.value === null ? "—" : formatCount(metric.value)}
                  </strong>
                  {metric.detail && <small>{metric.detail}</small>}
                </dd>
              </div>
            ))}
          </dl>
          <dl className="panel usage-counts">
            <div>
              <dt>模型调用</dt>
              <dd>{formatCount(summary.model_calls)}</dd>
            </div>
            <div>
              <dt>用量未知或待返回</dt>
              <dd>{formatCount(summary.unknown_calls)}</dd>
            </div>
            {summary.legacy_runs > 0 && (
              <div>
                <dt>已导入的历史任务</dt>
                <dd>{formatCount(summary.legacy_runs)}</dd>
              </div>
            )}
          </dl>
          {summary.legacy_runs > 0 && (
            <p className="usage-note">
              历史用量按任务创建日期计入，调用次数按启用统计后的记录计算。
            </p>
          )}
        </>
      )}
    </>
  );
}
