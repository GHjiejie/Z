import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { AlertTriangle, Info, ShieldCheck, UserRound } from "lucide-react";
import {
  aborted,
  ApiClient,
  type RoleCode,
  type RoleDefinition,
  type UserDraft,
  type UserRecord,
  type UserStatus,
} from "./api";
import {
  Avatar,
  ErrorBanner,
  Field,
  Modal,
  PasswordField,
  Spinner,
} from "./components";

export type UserDialogState =
  | { kind: "create" }
  | { kind: "edit" | "roles" | "reset" | "delete"; userId: string }
  | { kind: "status"; userId: string; nextStatus: UserStatus };

const initialDraft: UserDraft = {
  username: "",
  display_name: "",
  email: null,
  password: "",
  roles: ["member"],
};
const titles = {
  create: "新增用户",
  edit: "编辑用户",
  roles: "设置角色",
  reset: "重置密码",
  delete: "删除用户",
  status: "修改账号状态",
};

export function UserDialog({
  state,
  api,
  onClose,
  onSaved,
}: {
  state: UserDialogState;
  api: ApiClient;
  onClose: () => void;
  onSaved: (message: string, updated?: UserRecord) => void;
}) {
  const [record, setRecord] = useState<UserRecord | null>(null);
  const [draft, setDraft] = useState<UserDraft>(initialDraft);
  const [roleList, setRoleList] = useState<RoleDefinition[]>([]);
  const [confirmation, setConfirmation] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const formId = useId();
  const kind = state.kind;
  const userId = "userId" in state ? state.userId : null;
  const load = useCallback(() => {
    controller.current?.abort();
    const next = new AbortController();
    controller.current = next;
    setLoading(true);
    setLoadError(null);
    setError(null);
    setFormError("");
    async function run() {
      try {
        const [current, definitions] = await Promise.all([
          userId ? api.getUser(userId, next.signal) : Promise.resolve(null),
          kind === "create" || kind === "roles"
            ? api.roles(next.signal)
            : Promise.resolve([]),
        ]);
        if (next.signal.aborted) return;
        setRecord(current);
        setRoleList(definitions);
        setDraft(
          current
            ? {
                username: current.user.username,
                display_name: current.user.display_name,
                email: current.user.email,
                roles: current.user.roles,
                password: "",
              }
            : initialDraft,
        );
        setConfirmation("");
        setLoading(false);
      } catch (failure) {
        if (!aborted(failure) && !next.signal.aborted) {
          setLoadError(failure);
          setLoading(false);
        }
      }
    }
    void run();
  }, [api, kind, userId]);
  useEffect(() => {
    load();
    return () => controller.current?.abort();
  }, [load]);

  function toggleRole(code: RoleCode) {
    setDraft((value) => ({
      ...value,
      roles: value.roles.includes(code)
        ? value.roles.filter((item) => item !== code)
        : [...value.roles, code],
    }));
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || loading || loadError) return;
    setFormError("");
    setError(null);
    if (
      (kind === "create" || kind === "edit") &&
      (!draft.display_name.trim() || draft.display_name.trim().length > 100)
    ) {
      setFormError("请输入 1～100 字符的展示名称。");
      return;
    }
    if (
      kind === "create" &&
      !/^[A-Za-z0-9._-]{3,64}$/.test(draft.username.trim())
    ) {
      setFormError("用户名须为 3～64 位字母、数字、点、下划线或连字符。");
      return;
    }
    if ((kind === "create" || kind === "roles") && !draft.roles.length) {
      setFormError("请至少选择一个角色。");
      return;
    }
    if (
      (kind === "create" || kind === "reset") &&
      (Array.from(draft.password).length < 15 ||
        Array.from(draft.password).length > 128)
    ) {
      setFormError("密码长度须为 15～128 个字符。");
      return;
    }
    if (kind === "reset" && draft.password !== confirmation) {
      setFormError("两次输入的新密码不一致。");
      return;
    }
    setBusy(true);
    try {
      const profile = {
        display_name: draft.display_name.trim(),
        email: draft.email?.trim() || null,
      };
      let updated: UserRecord | undefined;
      let message = "";
      if (kind === "create") {
        updated = await api.createUser({
          ...draft,
          ...profile,
          username: draft.username.trim(),
        });
        message = "用户已创建，首次登录后需修改初始密码。";
      } else if (record) {
        if (kind === "edit") {
          updated = await api.profile(record, profile);
          message = "用户资料已更新。";
        }
        if (kind === "roles") {
          updated = await api.setRoles(record, draft.roles);
          message = "用户角色已更新，新权限立即生效。";
        }
        if (kind === "reset") {
          await api.resetPassword(record, draft.password);
          message = "密码已重置，该用户下次登录时需修改密码。";
        }
        if (kind === "delete") {
          await api.removeUser(record);
          message = "用户已删除。";
        }
        if (state.kind === "status") {
          updated = await api.setStatus(record, state.nextStatus);
          message =
            state.nextStatus === "active"
              ? "账号已启用，用户可重新登录。"
              : "账号已禁用，现有登录会话已撤销。";
        }
      }
      onSaved(message, updated);
    } catch (failure) {
      setError(failure);
      setBusy(false);
    }
  }
  const title =
    state.kind === "status"
      ? state.nextStatus === "active"
        ? "启用用户"
        : "禁用用户"
      : titles[kind];
  const action =
    kind === "create"
      ? "创建用户"
      : kind === "delete"
        ? "确认删除"
        : kind === "reset"
          ? "重置密码"
          : state.kind === "status"
            ? state.nextStatus === "active"
              ? "确认启用"
              : "确认禁用"
            : "保存更改";
  const destructive = kind === "delete";
  return (
    <Modal
      title={title}
      description={
        kind === "create" ? "创建账号，并为用户分配合适的访问角色。" : undefined
      }
      busy={busy}
      onClose={onClose}
      destructive={destructive}
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
            className={`button ${destructive ? "danger" : "primary"}`}
            disabled={busy || loading || Boolean(loadError)}
          >
            {busy ? <Spinner label="正在提交" /> : action}
          </button>
        </>
      }
    >
      {loading ? (
        <div className="modal-loading">
          <Spinner label="正在读取最新信息" />
        </div>
      ) : loadError ? (
        <ErrorBanner error={loadError} onRetry={load} />
      ) : (
        <form id={formId} onSubmit={submit}>
          {record && kind !== "edit" && (
            <div className="target-user">
              <Avatar user={record.user} />
              <div>
                <strong>{record.user.display_name}</strong>
                <small>{record.user.username}</small>
              </div>
            </div>
          )}
          {(kind === "create" || kind === "edit") && (
            <div className="form-stack">
              <Field
                label="用户名"
                hint={
                  kind === "create"
                    ? "用于登录，创建后不可修改。"
                    : "用户名创建后不可修改。"
                }
              >
                <input
                  name="username"
                  autoComplete="off"
                  required
                  readOnly={kind === "edit"}
                  maxLength={64}
                  value={draft.username}
                  placeholder="例如 zhangsan"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      username: event.target.value,
                    }))
                  }
                />
              </Field>
              <Field label="展示名称">
                <input
                  name="display_name"
                  required
                  maxLength={100}
                  value={draft.display_name}
                  placeholder="用户在系统中显示的名称"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      display_name: event.target.value,
                    }))
                  }
                />
              </Field>
              <Field label="联系邮箱（选填）">
                <input
                  name="email"
                  type="email"
                  maxLength={254}
                  value={draft.email || ""}
                  placeholder="name@example.com"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      email: event.target.value,
                    }))
                  }
                />
              </Field>
              {kind === "create" && (
                <PasswordField
                  label="初始密码"
                  autoComplete="new-password"
                  required
                  value={draft.password}
                  hint="15～128 字符，支持长口令与空格。"
                  onChange={(event) =>
                    setDraft((value) => ({
                      ...value,
                      password: event.target.value,
                    }))
                  }
                />
              )}
            </div>
          )}
          {(kind === "create" || kind === "roles") && (
            <fieldset className="role-options">
              <legend>访问角色</legend>
              {roleList.map((definition) => (
                <label
                  className={`role-option${draft.roles.includes(definition.code) ? " selected" : ""}`}
                  key={definition.code}
                >
                  <input
                    type="checkbox"
                    checked={draft.roles.includes(definition.code)}
                    onChange={() => toggleRole(definition.code)}
                  />
                  <span className="role-option-icon">
                    {definition.code === "admin" ? (
                      <ShieldCheck size={19} />
                    ) : (
                      <UserRound size={19} />
                    )}
                  </span>
                  <span>
                    <strong>{definition.display_name}</strong>
                    <small>{definition.description}</small>
                  </span>
                </label>
              ))}
            </fieldset>
          )}
          {kind === "reset" && (
            <div className="form-stack">
              <PasswordField
                label="新密码"
                value={draft.password}
                required
                autoComplete="new-password"
                hint="15～128 字符。请通过受控方式交付给用户。"
                onChange={(event) =>
                  setDraft((value) => ({
                    ...value,
                    password: event.target.value,
                  }))
                }
              />
              <PasswordField
                label="确认新密码"
                value={confirmation}
                required
                autoComplete="new-password"
                onChange={(event) => setConfirmation(event.target.value)}
              />
            </div>
          )}
          {kind === "delete" && (
            <div className="confirmation-copy">
              <span className="confirmation-icon">
                <AlertTriangle size={23} />
              </span>
              <p>确定删除这位用户吗？</p>
              <small>
                删除后，该用户将无法登录，现有登录会话会失效。已有业务历史会保留。
              </small>
            </div>
          )}
          {state.kind === "status" && (
            <p className="dialog-copy">
              {state.nextStatus === "disabled"
                ? "禁用后，该用户将无法登录，现有登录会话会立即撤销。你可以稍后重新启用此账号。"
                : "启用后，该用户可以使用原有密码重新登录。此前已撤销的会话不会恢复。"}
            </p>
          )}
          {(kind === "create" || kind === "reset" || kind === "roles") && (
            <div className="info-note">
              <Info size={16} />
              <span>
                {kind === "roles"
                  ? "角色权限取并集。调整后，用户的后续操作将使用新权限。"
                  : "用户首次使用此密码登录时，需要先设置自己的新密码。"}
              </span>
            </div>
          )}
          {formError && (
            <p className="form-error" role="alert">
              {formError}
            </p>
          )}
          {Boolean(error) && <ErrorBanner error={error} onReload={load} />}
        </form>
      )}
    </Modal>
  );
}
