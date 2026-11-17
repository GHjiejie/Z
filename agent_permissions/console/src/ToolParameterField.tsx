import { useEffect, useState } from "react";
import { Eye, EyeOff, LockKeyhole, Search } from "lucide-react";
import { aborted, errorMessage, type ApiClient } from "./api";
import type { ChatRun, ToolField, ToolOption } from "./chatRuns";

export interface ParameterDraft {
  included: boolean;
  nullValue: boolean;
  value: string | boolean | string[];
}

export function initialParameterDraft(field: ToolField): ParameterDraft {
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

export function ToolParameterField({
  field,
  draft,
  onChange,
  api,
  run,
  disabled,
  error,
}: {
  field: ToolField;
  draft: ParameterDraft;
  onChange: (draft: ParameterDraft) => void;
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
  const [passwordVisible, setPasswordVisible] = useState(false);
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

  const unavailable = disabled || !field.editable;
  const hasOptions = Boolean(field.source || field.options.length);
  const choiceList = field.type === "array" && hasOptions;
  const showControl = draft.included && !draft.nullValue;
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
  const hint = !draft.included
    ? "使用默认设置或保留原值"
    : draft.nullValue
      ? "本次提交将清空此值"
      : field.sensitive
        ? "仅用于本次操作，不保存到聊天记录"
        : field.type === "array" && !hasOptions
          ? "多个值用英文逗号分隔"
          : null;
  const describedBy =
    [hint ? `${id}-hint` : "", error ? `${id}-error` : ""]
      .filter(Boolean)
      .join(" ") || undefined;
  const label = (
    <>
      {field.label}
      {field.required && (
        <span className="tool-required" aria-label="必填">
          {" "}
          *
        </span>
      )}
    </>
  );

  return (
    <div
      className={`tool-form-field${choiceList || field.source === "users" ? " is-wide" : ""}${!draft.included ? " is-omitted" : ""}`}
    >
      <div className="tool-field-heading">
        {showControl && !choiceList ? (
          <label id={`${id}-label`} htmlFor={id}>
            {label}
          </label>
        ) : (
          <span id={`${id}-label`} className="tool-field-label">
            {label}
          </span>
        )}
        {!field.required && (
          <label className="tool-field-toggle">
            <input
              type="checkbox"
              aria-label={`填写${field.label}`}
              checked={draft.included}
              disabled={unavailable}
              onChange={(event) => {
                setPasswordVisible(false);
                onChange({ ...draft, included: event.target.checked });
              }}
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
            disabled={unavailable}
            onChange={(event) =>
              onChange({ ...draft, nullValue: event.target.checked })
            }
          />
          清空此值
        </label>
      )}
      {showControl && (
        <>
          {field.source === "users" && (
            <div className="tool-search-input">
              <Search size={16} aria-hidden="true" />
              <input
                type="search"
                aria-label="搜索目标用户"
                placeholder="按姓名或账号搜索"
                value={query}
                disabled={unavailable}
                onChange={(event) => setQuery(event.target.value)}
              />
            </div>
          )}
          {choiceList ? (
            <fieldset
              id={id}
              className="tool-option-list"
              aria-labelledby={`${id}-label`}
              aria-invalid={Boolean(error)}
              aria-describedby={describedBy}
              tabIndex={-1}
              disabled={unavailable || loading || Boolean(loadError)}
            >
              {visibleOptions.map((item) => (
                <label
                  className={`tool-option${selected.includes(item.value) ? " is-selected" : ""}`}
                  key={item.value}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(item.value)}
                    onChange={(event) =>
                      onChange({
                        ...draft,
                        value: event.target.checked
                          ? [...selected, item.value]
                          : selected.filter((value) => value !== item.value),
                      })
                    }
                  />
                  <span>{item.label}</span>
                </label>
              ))}
              {!visibleOptions.length && !loading && !loadError && (
                <small className="tool-empty-options">暂无可选项</small>
              )}
            </fieldset>
          ) : hasOptions ? (
            <select
              id={id}
              value={typeof draft.value === "string" ? draft.value : ""}
              disabled={unavailable || loading || Boolean(loadError)}
              required={field.required}
              aria-invalid={Boolean(error)}
              aria-describedby={describedBy}
              onChange={(event) =>
                onChange({ ...draft, value: event.target.value })
              }
            >
              <option value="">请选择{field.label}</option>
              {visibleOptions.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          ) : field.type === "boolean" ? (
            <label className="tool-boolean-control">
              <input
                id={id}
                type="checkbox"
                checked={draft.value === true}
                disabled={unavailable}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
                onChange={(event) =>
                  onChange({ ...draft, value: event.target.checked })
                }
              />
              <span>{draft.value === true ? "已开启" : "未开启"}</span>
            </label>
          ) : (
            <div
              className={
                field.sensitive ? "tool-secret-input" : "tool-text-input"
              }
            >
              <input
                id={id}
                type={
                  field.sensitive
                    ? passwordVisible
                      ? "text"
                      : "password"
                    : field.type === "number" || field.type === "integer"
                      ? "number"
                      : field.constraints.format === "email"
                        ? "email"
                        : "text"
                }
                placeholder={`请输入${field.label}`}
                value={
                  Array.isArray(draft.value)
                    ? draft.value.join(",")
                    : String(draft.value)
                }
                disabled={unavailable}
                autoComplete={field.sensitive ? "new-password" : "off"}
                required={field.required}
                min={numeric("minimum")}
                max={numeric("maximum")}
                step={field.type === "integer" ? 1 : "any"}
                minLength={numeric("minLength")}
                maxLength={numeric("maxLength")}
                aria-invalid={Boolean(error)}
                aria-describedby={describedBy}
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
              {field.sensitive && (
                <button
                  className="icon-button"
                  type="button"
                  aria-label={passwordVisible ? "隐藏密码" : "显示密码"}
                  aria-controls={id}
                  title={`${passwordVisible ? "隐藏" : "显示"}${field.label}`}
                  aria-pressed={passwordVisible}
                  disabled={unavailable}
                  onClick={() => setPasswordVisible((value) => !value)}
                >
                  {passwordVisible ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              )}
            </div>
          )}
        </>
      )}
      {hint && (
        <small id={`${id}-hint`} className="tool-field-help">
          {field.sensitive && showControl && (
            <LockKeyhole size={12} aria-hidden="true" />
          )}
          {hint}
        </small>
      )}
      {showControl && loading && <small role="status">正在读取可选项…</small>}
      {showControl &&
        hasOptions &&
        !choiceList &&
        !loading &&
        !loadError &&
        !visibleOptions.length && (
          <small role="status">
            {field.source === "users" && query
              ? "未找到匹配用户，请更换关键词"
              : "暂无可选项"}
          </small>
        )}
      {showControl && Boolean(loadError) && (
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
