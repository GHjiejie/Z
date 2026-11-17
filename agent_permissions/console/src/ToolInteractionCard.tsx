import { useEffect, useRef, useState } from "react";
import { Check, ShieldCheck } from "lucide-react";
import { ApiError, aborted, errorMessage, type ApiClient } from "./api";
import { chatId } from "./chatHistory";
import type {
  ChatRun,
  FieldValue,
  ToolField,
  ToolOption,
  ToolSubmission,
} from "./chatRuns";

interface Draft {
  included: boolean;
  nullValue: boolean;
  value: string | boolean | string[];
}

function initial(field: ToolField): Draft {
  return {
    included: field.required || field.present,
    nullValue: field.present && field.value === null,
    value:
      field.type === "array"
        ? Array.isArray(field.value)
          ? field.value
          : []
        : field.type === "boolean"
          ? field.value === true
          : field.value === undefined || field.value === null
            ? ""
            : String(field.value),
  };
}

function FieldControl({
  field,
  draft,
  onChange,
  api,
  run,
  disabled,
  error,
}: {
  field: ToolField;
  draft: Draft;
  onChange: (draft: Draft) => void;
  api: ApiClient;
  run: ChatRun;
  disabled: boolean;
  error?: string;
}) {
  const [options, setOptions] = useState<ToolOption[]>(field.options);
  const [query, setQuery] = useState("");
  const [loadError, setLoadError] = useState<unknown>(null);
  const [loading, setLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const id = `${run.interaction?.id}-${field.pointer.replace(/[^a-zA-Z0-9]/g, "_")}`;
  useEffect(() => {
    if (!field.source || !run.interaction || !draft.included || draft.nullValue)
      return;
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    const timer = window.setTimeout(
      () => {
        void api
          .interactionOptions(
            run.id,
            run.interaction?.id ?? "",
            field.pointer,
            query,
            controller.signal,
          )
          .then((values) => {
            if (!controller.signal.aborted) setOptions(values);
          })
          .catch((failure: unknown) => {
            if (!controller.signal.aborted && !aborted(failure))
              setLoadError(failure);
          })
          .finally(() => {
            if (!controller.signal.aborted) setLoading(false);
          });
      },
      query ? 200 : 0,
    );
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [
    api,
    run.id,
    run.interaction?.id,
    field.pointer,
    field.source,
    query,
    revision,
    draft.included,
    draft.nullValue,
  ]);

  const unavailable =
    disabled || !field.editable || !draft.included || draft.nullValue;
  const selected = Array.isArray(draft.value)
    ? draft.value
    : typeof draft.value === "string" && draft.value
      ? [draft.value]
      : [];
  const visibleOptions = [
    ...options,
    ...selected
      .filter((value) => !options.some((item) => item.value === value))
      .map((value) => ({ value, label: `${value}（当前值）` })),
  ];
  const numeric = (key: string) =>
    typeof field.constraints[key] === "number"
      ? field.constraints[key]
      : undefined;
  return (
    <div className="tool-form-field">
      <div className="tool-field-heading">
        <label htmlFor={id}>
          {field.label}
          {field.required && <span aria-label="必填"> *</span>}
        </label>
        {!field.required && (
          <label className="tool-field-toggle">
            <input
              type="checkbox"
              checked={draft.included}
              disabled={disabled || !field.editable}
              onChange={(event) =>
                onChange({ ...draft, included: event.target.checked })
              }
            />
            填写此项
          </label>
        )}
      </div>
      {field.nullable && draft.included && (
        <label className="tool-field-toggle">
          <input
            type="checkbox"
            checked={draft.nullValue}
            disabled={disabled}
            onChange={(event) =>
              onChange({ ...draft, nullValue: event.target.checked })
            }
          />
          清空此值
        </label>
      )}
      {field.source === "users" && draft.included && (
        <input
          aria-label="搜索目标用户"
          placeholder="按姓名或账号搜索"
          value={query}
          disabled={unavailable}
          onChange={(event) => setQuery(event.target.value)}
        />
      )}
      {field.source || field.options.length ? (
        <select
          id={id}
          multiple={field.type === "array"}
          value={
            field.type === "array"
              ? selected
              : typeof draft.value === "string"
                ? draft.value
                : ""
          }
          disabled={unavailable || loading || Boolean(loadError)}
          required={field.required && draft.included && !draft.nullValue}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `${id}-error` : undefined}
          onChange={(event) =>
            onChange({
              ...draft,
              value:
                field.type === "array"
                  ? Array.from(
                      event.target.selectedOptions,
                      (item) => item.value,
                    )
                  : event.target.value,
            })
          }
        >
          {field.type !== "array" && (
            <option value="">请选择{field.label}</option>
          )}
          {visibleOptions.map((item) => (
            <option key={item.value} value={item.value}>
              {item.label}
            </option>
          ))}
        </select>
      ) : field.type === "boolean" ? (
        <input
          id={id}
          type="checkbox"
          checked={draft.value === true}
          disabled={unavailable}
          onChange={(event) =>
            onChange({ ...draft, value: event.target.checked })
          }
        />
      ) : (
        <input
          id={id}
          type={
            field.sensitive
              ? "password"
              : field.type === "number" || field.type === "integer"
                ? "number"
                : field.constraints.format === "email"
                  ? "email"
                  : "text"
          }
          value={
            Array.isArray(draft.value)
              ? draft.value.join(",")
              : String(draft.value)
          }
          disabled={unavailable}
          autoComplete={field.sensitive ? "new-password" : "off"}
          required={field.required && draft.included && !draft.nullValue}
          min={numeric("minimum")}
          max={numeric("maximum")}
          step={field.type === "integer" ? 1 : "any"}
          minLength={numeric("minLength")}
          maxLength={numeric("maxLength")}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `${id}-error` : undefined}
          onChange={(event) =>
            onChange({
              ...draft,
              value:
                field.type === "array"
                  ? event.target.value.split(",").filter(Boolean)
                  : event.target.value,
            })
          }
        />
      )}
      {!draft.included && <small>不填写此项，使用默认设置或保留原值。</small>}
      {field.sensitive && draft.included && (
        <small>仅用于本次操作，不保存到聊天记录。</small>
      )}
      {loading && <small role="status">正在读取可选项…</small>}
      {Boolean(loadError) && (
        <div className="tool-field-error" role="alert">
          {errorMessage(loadError)}{" "}
          <button
            type="button"
            className="text-button"
            onClick={() => setRevision((value) => value + 1)}
          >
            重试选项
          </button>
        </div>
      )}
      {error && (
        <small id={`${id}-error`} className="tool-field-error" role="alert">
          {error}
        </small>
      )}
    </div>
  );
}

export function ToolInteractionCard({
  api,
  run,
  busy,
  onSubmit,
  onCancel,
  onRefresh,
  onRecover,
}: {
  api: ApiClient;
  run: ChatRun;
  busy: boolean;
  onSubmit: (payload: ToolSubmission) => Promise<void>;
  onCancel: () => Promise<void>;
  onRefresh: (requestId: string) => Promise<void>;
  onRecover: () => void;
}) {
  const interaction = run.interaction;
  const [draft, setDraft] = useState<Record<string, Draft>>(() =>
    Object.fromEntries(
      (interaction?.fields ?? []).map((field) => [
        field.pointer,
        initial(field),
      ]),
    ),
  );
  const [error, setError] = useState<unknown>(null);
  const [working, setWorking] = useState(false);
  const locked = useRef(false);
  const mounted = useRef(true);
  const form = useRef<HTMLFormElement>(null);
  const submission = useRef<{ fingerprint: string; id: string } | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    form.current
      ?.querySelector<HTMLInputElement>("input[required]:not([disabled])")
      ?.focus({ preventScroll: true });
  }, []);
  if (!interaction) return null;
  const disabled =
    working || busy || !["waiting", "interrupted"].includes(run.status);
  const conflict =
    error instanceof ApiError &&
    [
      "RESOURCE_VERSION_CONFLICT",
      "TOOL_VERSION_MISMATCH",
      "INTERACTION_VERSION_CONFLICT",
      "INTERACTION_ALREADY_RESOLVED",
    ].includes(error.code);
  const errors =
    error instanceof ApiError
      ? Object.fromEntries(
          error.details.map((item) => [item.field, item.message]),
        )
      : {};
  async function act(action: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setWorking(true);
    setError(null);
    try {
      await action();
    } catch (failure) {
      if (mounted.current && !aborted(failure)) setError(failure);
    } finally {
      locked.current = false;
      if (mounted.current) setWorking(false);
    }
  }
  async function submit() {
    if (!interaction) return;
    const values: Record<string, FieldValue> = {};
    const sensitive: Record<string, FieldValue> = {};
    const omit: string[] = [];
    for (const field of interaction.fields) {
      if (!field.editable) continue;
      const current = draft[field.pointer] ?? initial(field);
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
  const target = interaction.target;
  return (
    <form
      ref={form}
      className="tool-interaction-card"
      aria-label={`${interaction.title}参数确认`}
      onSubmit={(event) => {
        event.preventDefault();
        void act(submit);
      }}
    >
      <div className="tool-card-heading">
        <ShieldCheck size={20} aria-hidden="true" />
        <div>
          <h3>{interaction.title}</h3>
          <span>
            {interaction.kind === "collect_parameters"
              ? "等待补充参数"
              : "等待执行确认"}
          </span>
        </div>
      </div>
      <p>{interaction.description}</p>
      {target && (
        <div className="tool-target">
          <strong>操作对象</strong>
          <span>
            {String(
              target.display_name ?? target.username ?? target.code ?? "",
            )}
            {target.username || target.code
              ? `（${String(target.username ?? target.code)}）`
              : ""}
          </span>
        </div>
      )}
      {interaction.effect === "destructive" && (
        <p className="tool-danger-note">
          此操作可能删除数据或使账号登录失效，请核对操作对象。
        </p>
      )}
      <div className="tool-form-grid">
        {interaction.fields
          .filter((field) => field.editable || !target)
          .map((field) => (
            <FieldControl
              key={field.pointer}
              field={field}
              draft={draft[field.pointer] ?? initial(field)}
              onChange={(value) => {
                submission.current = null;
                setDraft((current) => ({ ...current, [field.pointer]: value }));
              }}
              api={api}
              run={run}
              disabled={disabled}
              error={errors[field.pointer]}
            />
          ))}
      </div>
      {Boolean(error) && (
        <div className="tool-card-error" role="alert">
          <p>{errorMessage(error)}</p>
          {conflict ? (
            <button
              className="text-button"
              type="button"
              disabled={working}
              onClick={() => void act(() => onRefresh(chatId()))}
            >
              重新准备并核对参数
            </button>
          ) : (
            <button className="text-button" type="button" onClick={onRecover}>
              读取最新任务状态
            </button>
          )}
        </div>
      )}
      <div className="tool-card-actions">
        <button
          className="button secondary"
          type="button"
          disabled={disabled}
          onClick={() => void act(onCancel)}
        >
          取消操作
        </button>
        <button
          className={`button ${interaction.effect === "destructive" ? "danger" : "primary"}`}
          disabled={disabled || conflict}
          type="submit"
        >
          <Check size={15} />
          {working || busy
            ? "正在提交…"
            : interaction.confirmation_required
              ? "确认执行"
              : "提交并查询"}
        </button>
      </div>
      <small className="tool-card-expiry">
        本次确认仅用于当前操作；
        {new Date(interaction.expires_at * 1000).toLocaleTimeString("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
        })}{" "}
        前有效。
      </small>
    </form>
  );
}
