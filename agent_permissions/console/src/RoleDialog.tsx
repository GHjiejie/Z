import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  aborted,
  ApiClient,
  type PermissionDefinition,
  type RoleDraft,
  type RoleRecord,
} from "./api";
import { ErrorBanner, Field, Modal, Spinner } from "./components";

export type RoleDialogState =
  { kind: "create" } | { kind: "edit" | "delete"; code: string };

export function RoleDialog({
  state,
  api,
  granted,
  onClose,
  onSaved,
}: {
  state: RoleDialogState;
  api: ApiClient;
  granted: string[];
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const formId = useId();
  const [record, setRecord] = useState<RoleRecord | null>(null);
  const [catalog, setCatalog] = useState<PermissionDefinition[]>([]);
  const [draft, setDraft] = useState<RoleDraft>({
    code: "",
    display_name: "",
    description: "",
    permissions: [],
  });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const kind = state.kind;
  const code = "code" in state ? state.code : null;
  const initialPermissions = useRef(
    granted.filter((value) =>
      [
        "user.self.read",
        "user.self.update",
        "user.self.password.change",
      ].includes(value),
    ),
  );
  const load = useCallback(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setLoading(true);
    setLoadError(null);
    setError(null);
    setFormError("");
    void Promise.all([
      code ? api.getRole(code, next.signal) : Promise.resolve(null),
      kind === "delete"
        ? Promise.resolve([])
        : api.permissionCatalog(next.signal),
    ])
      .then(([current, permissions]) => {
        if (next.signal.aborted) return;
        setRecord(current);
        setCatalog(permissions);
        setDraft(
          current
            ? { ...current.role }
            : {
                code: "",
                display_name: "",
                description: "",
                permissions: initialPermissions.current,
              },
        );
        setLoading(false);
      })
      .catch((failure) => {
        if (!aborted(failure) && !next.signal.aborted) {
          setLoadError(failure);
          setLoading(false);
        }
      });
  }, [api, code, kind]);
  useEffect(() => {
    load();
    return () => controller.current?.abort();
  }, [load]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || loading || loadError) return;
    if (
      kind !== "delete" &&
      (!draft.display_name.trim() ||
        (kind === "create" && !/^[a-z][a-z0-9_-]{2,63}$/.test(draft.code)))
    ) {
      setFormError(
        "请填写展示名称，以及 3～64 位小写字母、数字、下划线或连字符的角色编码",
      );
      return;
    }
    setBusy(true);
    setError(null);
    setFormError("");
    try {
      const value = {
        ...draft,
        display_name: draft.display_name.trim(),
        description: draft.description.trim(),
      };
      if (kind === "create") await api.createRole(value);
      else if (record && kind === "edit") await api.updateRole(record, value);
      else if (record) await api.deleteRole(record);
      onSaved(
        kind === "create"
          ? "角色已创建，可在用户管理中分配"
          : kind === "delete"
            ? "角色已删除"
            : "角色权限已更新，后续请求立即生效",
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  const groups = [...new Set(catalog.map((value) => value.resource))];
  return (
    <Modal
      title={
        kind === "create"
          ? "新增角色"
          : kind === "edit"
            ? "编辑角色"
            : "删除角色"
      }
      description={
        kind === "delete" ? undefined : "用户通过角色获得所选操作权限"
      }
      busy={busy}
      onClose={onClose}
      destructive={kind === "delete"}
      footer={
        <>
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            取消
          </button>
          <button
            type="submit"
            form={formId}
            className={`button ${kind === "delete" ? "danger" : "primary"}`}
            disabled={
              busy ||
              loading ||
              Boolean(loadError) ||
              (kind === "delete" && Boolean(record?.role.user_count))
            }
          >
            {busy ? (
              <Spinner label="正在提交" />
            ) : kind === "create" ? (
              "创建角色"
            ) : kind === "delete" ? (
              "确认删除"
            ) : (
              "保存角色"
            )}
          </button>
        </>
      }
    >
      {loading ? (
        <Spinner label="正在加载角色定义" />
      ) : loadError ? (
        <ErrorBanner error={loadError} onRetry={load} />
      ) : (
        <form id={formId} className="form-stack" onSubmit={submit}>
          {kind === "delete" ? (
            <div className="confirmation-copy">
              <p>确定删除“{record?.role.display_name}”吗？</p>
              <small>
                {record?.role.user_count
                  ? "此角色仍分配给用户，请先在用户管理中调整角色"
                  : "删除后，该角色将不再出现在用户的角色选项中"}
              </small>
            </div>
          ) : (
            <fieldset className="role-editor-fields" disabled={busy}>
              <Field label="角色编码" hint="创建后不可修改，以小写字母开头">
                <input
                  required
                  value={draft.code}
                  readOnly={kind === "edit"}
                  maxLength={64}
                  pattern="[a-z][a-z0-9_-]{2,63}"
                  placeholder="例如 user_auditor"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      code: event.target.value,
                    }))
                  }
                />
              </Field>
              <Field label="角色名称">
                <input
                  required
                  value={draft.display_name}
                  maxLength={100}
                  placeholder="例如 用户审计员"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      display_name: event.target.value,
                    }))
                  }
                />
              </Field>
              <Field label="角色说明">
                <textarea
                  value={draft.description}
                  maxLength={500}
                  rows={2}
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      description: event.target.value,
                    }))
                  }
                />
              </Field>
              <div className="permission-picker">
                <h3>
                  操作权限{" "}
                  <span className="count-badge">
                    已选 {draft.permissions.length} 项
                  </span>
                </h3>
                <p className="field-hint">
                  访问 Console
                  需要“查看自己的信息与权限”。你只能授予自己已有的权限。
                </p>
                {groups.map((group) => (
                  <fieldset className="permission-group" key={group}>
                    <legend>{group}</legend>
                    {catalog
                      .filter((value) => value.resource === group)
                      .map((permission) => (
                        <label
                          className="permission-choice"
                          key={permission.code}
                        >
                          <input
                            type="checkbox"
                            checked={draft.permissions.includes(
                              permission.code,
                            )}
                            disabled={!granted.includes(permission.code)}
                            onChange={(event) => {
                              const checked = event.target.checked;
                              setDraft((value) => ({
                                ...value,
                                permissions: checked
                                  ? [...value.permissions, permission.code]
                                  : value.permissions.filter(
                                      (item) => item !== permission.code,
                                    ),
                              }));
                            }}
                          />
                          <span>
                            {permission.display_name}
                            <small>{permission.code}</small>
                          </span>
                        </label>
                      ))}
                  </fieldset>
                ))}
              </div>
            </fieldset>
          )}
          {formError && (
            <p className="field-error" role="alert">
              {formError}
            </p>
          )}
          {Boolean(error) && <ErrorBanner error={error} onReload={load} />}
        </form>
      )}
    </Modal>
  );
}
