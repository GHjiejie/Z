import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronDown,
  Clock3,
  LoaderCircle,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { ApiError, aborted, errorMessage, type ApiClient } from "./api";
import { chatId } from "./chatHistory";
import type {
  ChatRun,
  FieldValue,
  ToolField,
  ToolSubmission,
} from "./chatRuns";
import {
  initialParameterDraft,
  ToolParameterField,
  type ParameterDraft,
} from "./ToolParameterField";

export function ToolInteractionCard({
  api,
  run,
  busy,
  onSubmit,
  onRefresh,
  onRecover,
}: {
  api: ApiClient;
  run: ChatRun;
  busy: boolean;
  onSubmit: (payload: ToolSubmission) => Promise<void>;
  onRefresh: (requestId: string) => Promise<void>;
  onRecover: () => void;
}) {
  const interaction = run.interaction;
  const [draft, setDraft] = useState<Record<string, ParameterDraft>>(() =>
    Object.fromEntries(
      (interaction?.fields ?? []).map((field) => [
        field.pointer,
        initialParameterDraft(field),
      ]),
    ),
  );
  const [error, setError] = useState<unknown>(null);
  const [pendingAction, setPendingAction] = useState<
    "submit" | "cancel" | "refresh" | null
  >(null);
  const [optionalOpen, setOptionalOpen] = useState(() => {
    const fields = (interaction?.fields ?? []).filter(
      (field) => field.editable || !interaction?.target,
    );
    return (
      !fields.some((field) => field.required) ||
      fields.some((field) => !field.required && field.present)
    );
  });
  const working = pendingAction !== null;
  const locked = useRef(false);
  const mounted = useRef(true);
  const form = useRef<HTMLFormElement>(null);
  const submission = useRef<{ fingerprint: string; id: string } | null>(null);
  const cancellationId = useRef<string | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    form.current
      ?.querySelector<HTMLElement>(
        "input[required]:not(:disabled), select[required]:not(:disabled)",
      )
      ?.focus({ preventScroll: true });
  }, []);
  useEffect(() => {
    if (!(error instanceof ApiError)) return;
    const pointers = new Set(error.details.map((item) => item.field));
    if (
      interaction?.fields.some(
        (field) => !field.required && pointers.has(field.pointer),
      )
    )
      setOptionalOpen(true);
    const frame = requestAnimationFrame(() =>
      form.current
        ?.querySelector<HTMLElement>('[aria-invalid="true"]')
        ?.focus(),
    );
    return () => cancelAnimationFrame(frame);
  }, [error, interaction]);
  if (!interaction) return null;
  const disabled =
    working || busy || !["waiting", "interrupted"].includes(run.status);
  const stale =
    error instanceof ApiError &&
    [
      "INTERACTION_VERSION_CONFLICT",
      "INTERACTION_ALREADY_RESOLVED",
      "INTERACTION_EXPIRED",
    ].includes(error.code);
  const needsNewProposal =
    error instanceof ApiError &&
    ["RESOURCE_VERSION_CONFLICT", "TOOL_VERSION_MISMATCH"].includes(error.code);
  const errors =
    error instanceof ApiError
      ? Object.fromEntries(
          error.details.map((item) => [item.field, item.message]),
        )
      : {};
  async function act(
    kind: "submit" | "cancel" | "refresh",
    action: () => Promise<void>,
  ) {
    if (locked.current) return;
    locked.current = true;
    setPendingAction(kind);
    setError(null);
    try {
      await action();
    } catch (failure) {
      if (mounted.current && !aborted(failure)) setError(failure);
    } finally {
      locked.current = false;
      if (mounted.current) setPendingAction(null);
    }
  }
  async function submit() {
    if (!interaction) return;
    const values: Record<string, FieldValue> = {};
    const sensitive: Record<string, FieldValue> = {};
    const omit: string[] = [];
    for (const field of interaction.fields) {
      if (!field.editable) continue;
      const current = draft[field.pointer] ?? initialParameterDraft(field);
      if (!current.included) {
        if (!field.required) omit.push(field.pointer);
        continue;
      }
      let value: FieldValue = current.nullValue ? null : current.value;
      if (
        !current.nullValue &&
        (field.type === "number" || field.type === "integer")
      ) {
        if (
          typeof current.value !== "string" ||
          !current.value.trim() ||
          !Number.isFinite(Number(current.value))
        )
          throw new ApiError(`请填写有效的${field.label}`, "VALIDATION_ERROR");
        value = Number(current.value);
      }
      (field.sensitive ? sensitive : values)[field.pointer] = value;
    }
    const fingerprint = JSON.stringify({ values, omit });
    if (!submission.current || submission.current.fingerprint !== fingerprint)
      submission.current = { fingerprint, id: chatId() };
    await onSubmit({
      submission_id: submission.current.id,
      expected_version: interaction.version,
      decision: "submit",
      values,
      sensitive_values: sensitive,
      omit,
    });
  }
  async function cancel() {
    if (!interaction) return;
    cancellationId.current ??= chatId();
    await onSubmit({
      submission_id: cancellationId.current,
      expected_version: interaction.version,
      decision: "cancel",
      values: {},
      sensitive_values: {},
      omit: [],
    });
  }
  const target = interaction.target;
  const fields = interaction.fields.filter(
    (field) => field.editable || !target,
  );
  const requiredFields = fields.filter((field) => field.required);
  const optionalFields = fields.filter((field) => !field.required);
  const includedOptions = optionalFields.filter(
    (field) => (draft[field.pointer] ?? initialParameterDraft(field)).included,
  ).length;
  const targetName = target
    ? String(target.display_name ?? target.username ?? target.code ?? "")
    : "";
  const targetCode = target ? String(target.username ?? target.code ?? "") : "";
  const expiresAt = new Date(interaction.expires_at * 1000);
  function parameter(field: ToolField) {
    return (
      <ToolParameterField
        key={field.pointer}
        field={field}
        draft={draft[field.pointer] ?? initialParameterDraft(field)}
        onChange={(value) => {
          submission.current = null;
          setDraft((current) => ({ ...current, [field.pointer]: value }));
        }}
        api={api}
        run={run}
        disabled={disabled}
        error={errors[field.pointer]}
      />
    );
  }
  return (
    <form
      ref={form}
      className="tool-interaction-card"
      data-effect={interaction.effect}
      aria-label={`${interaction.title}参数确认`}
      aria-busy={working || busy}
      onInvalid={(event) => {
        const control = event.target;
        if (
          !optionalOpen &&
          (control instanceof HTMLInputElement ||
            control instanceof HTMLSelectElement) &&
          control.closest(".tool-optional-fields")
        ) {
          event.preventDefault();
          setOptionalOpen(true);
          requestAnimationFrame(() => {
            control.focus();
            control.reportValidity();
          });
        }
      }}
      onSubmit={(event) => {
        event.preventDefault();
        void act("submit", submit);
      }}
    >
      <header className="tool-card-header">
        <div className="tool-card-heading">
          <span className="tool-card-icon">
            <ShieldCheck size={18} aria-hidden="true" />
          </span>
          <h3>{interaction.title}</h3>
          <span className="tool-card-status">
            {working || busy
              ? "正在处理"
              : interaction.kind === "collect_parameters"
                ? "等待补充参数"
                : "等待执行确认"}
          </span>
        </div>
        <p className="tool-card-description">{interaction.description}</p>
      </header>
      <div className="tool-card-body">
        {target && (
          <div className="tool-target">
            <span className="tool-target-label">操作对象</span>
            <div>
              <strong>{targetName}</strong>
              {targetCode && targetCode !== targetName && (
                <span className="tool-target-code">{targetCode}</span>
              )}
            </div>
          </div>
        )}
        {interaction.effect === "destructive" && (
          <div className="tool-danger-note" role="note">
            <TriangleAlert size={16} aria-hidden="true" />
            <p>此操作可能删除数据或使账号登录失效，确认前请核对操作对象</p>
          </div>
        )}
        {requiredFields.length > 0 && (
          <section className="tool-parameter-section" aria-label="必填参数">
            <div className="tool-section-heading">
              <h4>填写参数</h4>
              <span>{requiredFields.length} 项必填</span>
            </div>
            <div className="tool-form-grid">
              {requiredFields.map(parameter)}
            </div>
          </section>
        )}
        {optionalFields.length > 0 && (
          <details
            className="tool-optional-fields"
            open={optionalOpen}
            onToggle={(event) => setOptionalOpen(event.currentTarget.open)}
          >
            <summary>
              <span className="tool-optional-title">
                可选参数 <span>{optionalFields.length} 项</span>
              </span>
              <span className="tool-optional-count">
                {includedOptions ? `已填写 ${includedOptions} 项` : "按需填写"}
              </span>
              <ChevronDown size={16} aria-hidden="true" />
            </summary>
            <div className="tool-optional-body">
              <p>未勾选的参数会使用默认设置或保留原值</p>
              <div className="tool-form-grid">
                {optionalFields.map(parameter)}
              </div>
            </div>
          </details>
        )}
        {Boolean(error) && (
          <div className="tool-card-error" role="alert">
            <p>{errorMessage(error)}</p>
            {needsNewProposal ? (
              <button
                className="text-button"
                type="button"
                disabled={working}
                onClick={() => void act("refresh", () => onRefresh(chatId()))}
              >
                {pendingAction === "refresh"
                  ? "正在重新准备…"
                  : "重新准备并核对参数"}
              </button>
            ) : (
              <button
                className="text-button"
                type="button"
                disabled={working || busy}
                onClick={onRecover}
              >
                读取最新任务状态
              </button>
            )}
          </div>
        )}
      </div>
      <footer className="tool-card-footer">
        <div className="tool-card-expiry">
          <Clock3 size={16} aria-hidden="true" />
          <div>
            <span>仅对本次操作有效</span>
            <time dateTime={expiresAt.toISOString()}>
              {new Intl.DateTimeFormat("zh-CN", {
                timeZone: "Asia/Shanghai",
                hour: "2-digit",
                minute: "2-digit",
              }).format(expiresAt)}{" "}
              前有效
            </time>
          </div>
        </div>
        <div className="tool-card-actions">
          <button
            className="button secondary"
            type="button"
            disabled={disabled || stale}
            onClick={() => void act("cancel", cancel)}
          >
            {pendingAction === "cancel" && (
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
            )}
            {pendingAction === "cancel" ? "正在取消…" : "取消操作"}
          </button>
          <button
            className={`button ${interaction.effect === "destructive" ? "danger" : "primary"}`}
            disabled={disabled || stale || needsNewProposal}
            type="submit"
          >
            {pendingAction === "submit" || busy ? (
              <LoaderCircle size={16} className="spin" aria-hidden="true" />
            ) : (
              <Check size={16} aria-hidden="true" />
            )}
            {pendingAction === "submit" || busy
              ? interaction.confirmation_required
                ? "正在执行…"
                : "正在查询…"
              : interaction.confirmation_required
                ? "确认执行"
                : "提交并查询"}
          </button>
        </div>
      </footer>
    </form>
  );
}
