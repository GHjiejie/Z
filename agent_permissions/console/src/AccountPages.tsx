import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowRight,
  KeyRound,
  LockKeyhole,
  ShieldCheck,
  UserRound,
} from "lucide-react";
import {
  aborted,
  ApiClient,
  ApiError,
  type RoleDefinition,
  type UserRecord,
} from "./api";
import {
  Avatar,
  Brand,
  ErrorBanner,
  Field,
  PermissionList,
  PasswordField,
  RoleBadge,
  Spinner,
  formatDate,
} from "./components";

export function LoginPage({
  notice,
  onLogin,
}: {
  notice: string;
  onLogin: (username: string, password: string) => Promise<void>;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await onLogin(username.trim(), password);
    } catch (failure) {
      setError(failure);
      setBusy(false);
    }
  }
  return (
    <div className="auth-layout">
      <aside className="auth-story">
        <Brand />
        <div className="auth-story-main">
          <span className="story-icon">
            <ShieldCheck size={18} />
          </span>
          <h1>
            用户与角色
            <br />
            在一处管理
          </h1>
          <p>
            集中维护成员身份与角色
            <br />
            明确每个账号允许执行的操作
          </p>
          <div className="identity-diagram" aria-hidden="true">
            <div>
              <span className="diagram-icon">
                <UserRound size={18} />
              </span>
              <span>用户身份</span>
            </div>
            <span className="diagram-line" />
            <div>
              <span className="diagram-icon">
                <ShieldCheck size={18} />
              </span>
              <span>访问角色</span>
            </div>
            <span className="diagram-line" />
            <div>
              <span className="diagram-icon">
                <KeyRound size={18} />
              </span>
              <span>操作权限</span>
            </div>
          </div>
        </div>
        <footer>
          Agent Console<span>用户与权限管理</span>
        </footer>
      </aside>
      <main className="auth-main">
        <div className="auth-card">
          <div className="login-brand">
            <Brand compact />
          </div>
          <span className="login-icon">
            <LockKeyhole size={18} />
          </span>
          <h2>登录 Agent Console</h2>
          <p className="auth-subtitle">使用你的系统账号登录</p>
          {notice && (
            <div className="success-note" role="status">
              {notice}
            </div>
          )}
          <form onSubmit={submit} className="form-stack">
            <Field label="用户名">
              <input
                name="username"
                autoComplete="username"
                required
                autoFocus
                maxLength={64}
                placeholder="请输入用户名"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                disabled={busy}
              />
            </Field>
            <PasswordField
              label="密码"
              autoComplete="current-password"
              required
              placeholder="请输入密码"
              value={password}
              disabled={busy}
              onChange={(event) => setPassword(event.target.value)}
            />
            {Boolean(error) && <ErrorBanner error={error} />}
            <button
              type="submit"
              className="button primary login-submit"
              disabled={busy}
            >
              {busy ? (
                <Spinner label="正在登录" />
              ) : (
                <>
                  登录控制台
                  <ArrowRight size={16} />
                </>
              )}
            </button>
          </form>
          <div className="auth-help">
            <ShieldCheck size={16} />
            <span>账号由管理员创建。如需帮助，请联系系统管理员。</span>
          </div>
        </div>
        <footer className="auth-footer">Agent Console · 身份与访问管理</footer>
      </main>
    </div>
  );
}

export function PasswordForm({
  api,
  initial,
  onDone,
  forced = false,
}: {
  api: ApiClient;
  initial: UserRecord;
  onDone: () => void;
  forced?: boolean;
}) {
  const [record, setRecord] = useState(initial);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [validation, setValidation] = useState("");
  useEffect(() => {
    setRecord(initial);
  }, [initial]);
  async function reload() {
    setReloading(true);
    try {
      setRecord(await api.me());
      setError(null);
    } catch (failure) {
      setError(failure);
    } finally {
      setReloading(false);
    }
  }
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || reloading) return;
    setError(null);
    setValidation("");
    if (Array.from(next).length < 15 || Array.from(next).length > 128) {
      setValidation("新密码长度须为 15～128 个字符");
      return;
    }
    if (next !== confirm) {
      setValidation("两次输入的新密码不一致");
      return;
    }
    if (current === next) {
      setValidation("新密码不能与当前密码相同");
      return;
    }
    setBusy(true);
    try {
      await api.changePassword(record, current, next);
      onDone();
    } catch (failure) {
      setError(failure);
      setBusy(false);
    }
  }
  return (
    <form className="form-stack password-form" onSubmit={submit}>
      <PasswordField
        label={forced ? "初始密码" : "当前密码"}
        autoComplete="current-password"
        required
        value={current}
        disabled={busy}
        onChange={(event) => setCurrent(event.target.value)}
      />
      <PasswordField
        label="新密码"
        autoComplete="new-password"
        required
        value={next}
        hint="15～128 字符，支持空格与长口令"
        disabled={busy}
        onChange={(event) => setNext(event.target.value)}
      />
      <PasswordField
        label="确认新密码"
        autoComplete="new-password"
        required
        value={confirm}
        disabled={busy}
        onChange={(event) => setConfirm(event.target.value)}
      />
      {validation && (
        <p className="form-error" role="alert">
          {validation}
        </p>
      )}
      {Boolean(error) && (
        <ErrorBanner
          error={error}
          onReload={() => {
            void reload();
          }}
        />
      )}
      <div className="password-note">
        <LockKeyhole size={16} />
        <span>修改后将退出所有登录会话，请使用新密码重新登录</span>
      </div>
      <button
        className="button primary"
        type="submit"
        disabled={busy || reloading}
      >
        {busy || reloading ? (
          <Spinner label={reloading ? "正在读取最新信息" : "正在修改密码"} />
        ) : forced ? (
          "设置新密码"
        ) : (
          "更新密码"
        )}
      </button>
    </form>
  );
}

export function FirstPasswordPage({
  api,
  record,
  onDone,
  onLogout,
}: {
  api: ApiClient;
  record: UserRecord;
  onDone: () => void;
  onLogout: () => void;
}) {
  return (
    <main className="first-password-page">
      <Brand />
      <section className="first-password-card">
        <span className="login-icon">
          <KeyRound size={18} />
        </span>
        <span className="step-badge">首次登录 · 账号安全</span>
        <h1>设置你的新密码</h1>
        <p>
          你好，{record.user.display_name}
          。为保护你的账号，请先将初始密码修改为仅你知道的新密码。
        </p>
        <PasswordForm api={api} initial={record} onDone={onDone} forced />
        <button className="text-button return-login" onClick={onLogout}>
          <ArrowLeft size={16} />
          退出当前登录
        </button>
      </section>
      <footer>Agent Console · 身份与访问管理</footer>
    </main>
  );
}

export function RolesPage({ api }: { api: ApiClient }) {
  const [roles, setRoles] = useState<RoleDefinition[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    api
      .roles(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setRoles(data);
      })
      .catch((failure) => {
        if (!aborted(failure) && !controller.signal.aborted) setError(failure);
      });
    return () => controller.abort();
  }, [api, revision]);
  return (
    <>
      <header className="page-heading">
        <div>
          <div className="eyebrow">访问控制</div>
          <h1>角色与权限</h1>
          <p>核对系统角色允许的操作，再为成员分配权限</p>
        </div>
        <span className="built-in-label">
          <ShieldCheck size={16} />
          系统内置角色
        </span>
      </header>
      {Boolean(error) && (
        <ErrorBanner
          error={error}
          onRetry={() => setRevision((value) => value + 1)}
        />
      )}
      {!roles && !error && (
        <section className="panel center-loading">
          <Spinner label="正在加载角色权限" />
        </section>
      )}
      <div className="roles-grid">
        {roles?.map((role) => (
          <section className="panel role-card" key={role.code}>
            <div className="role-card-icon">
              {role.code === "admin" ? (
                <ShieldCheck size={18} />
              ) : (
                <UserRound size={18} />
              )}
            </div>
            <div className="role-card-title">
              <h2>{role.display_name}</h2>
              <span className="count-badge">
                {role.permissions.length} 项权限
              </span>
            </div>
            <p>{role.description}</p>
            <div className="role-card-divider" />
            <h3>允许的操作</h3>
            <PermissionList permissions={role.permissions} />
          </section>
        ))}
      </div>
      <div className="role-explanation">
        <ShieldCheck size={18} />
        <div>
          <h3>权限随角色分配，操作在服务端校验</h3>
          <p>
            一个用户可以拥有多个角色，权限取各角色的并集。角色调整将应用于用户的后续请求；已禁用的账号无法继续访问系统。
          </p>
        </div>
      </div>
    </>
  );
}

export function ProfilePage({
  api,
  record,
  permissions,
  notify,
  onChanged,
  onPasswordDone,
}: {
  api: ApiClient;
  record: UserRecord;
  permissions: string[];
  notify: (message: string) => void;
  onChanged: (record: UserRecord) => void;
  onPasswordDone: () => void;
}) {
  const [name, setName] = useState(record.user.display_name);
  const [email, setEmail] = useState(record.user.email || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [editRecord, setEditRecord] = useState(record);
  const reload = useCallback(async () => {
    setBusy(true);
    try {
      const current = await api.me();
      setEditRecord(current);
      setName(current.user.display_name);
      setEmail(current.user.email || "");
      setError(null);
      onChanged(current);
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }, [api, onChanged]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    if (!name.trim()) {
      setError(new ApiError("请输入展示名称", "VALIDATION_ERROR"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const current = await api.profile(
        editRecord,
        { display_name: name.trim(), email: email.trim() || null },
        true,
      );
      setEditRecord(current);
      onChanged(current);
      notify("个人资料已更新");
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <header className="page-heading">
        <div>
          <div className="eyebrow">我的账号</div>
          <h1>个人设置</h1>
          <p>维护个人资料与账号安全</p>
        </div>
      </header>
      <div className="profile-layout">
        <aside className="panel profile-summary">
          <Avatar user={record.user} large />
          <h2>{record.user.display_name}</h2>
          <p>{record.user.username}</p>
          <RoleBadge admin={record.user.roles.includes("admin")} />
          <div className="summary-divider" />
          <dl>
            <div>
              <dt>账号状态</dt>
              <dd>已启用</dd>
            </div>
            <div>
              <dt>创建时间</dt>
              <dd>{formatDate(record.user.created_at, true)}</dd>
            </div>
          </dl>
          <h3>我的权限</h3>
          <PermissionList permissions={permissions} />
        </aside>
        <div className="profile-panels">
          <section className="panel settings-panel">
            <div className="settings-heading">
              <span>
                <UserRound size={18} />
              </span>
              <div>
                <h2>基本资料</h2>
                <p>用于在系统中识别你的身份</p>
              </div>
            </div>
            <form className="form-stack" onSubmit={submit}>
              <Field label="用户名" hint="登录名创建后不可修改">
                <input value={record.user.username} readOnly />
              </Field>
              <Field label="展示名称">
                <input
                  required
                  maxLength={100}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                />
              </Field>
              <Field label="联系邮箱（选填）">
                <input
                  type="email"
                  maxLength={254}
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="name@example.com"
                />
              </Field>
              {Boolean(error) && (
                <ErrorBanner
                  error={error}
                  onReload={() => {
                    void reload();
                  }}
                />
              )}
              <div>
                <button
                  type="submit"
                  className="button primary"
                  disabled={busy}
                >
                  {busy ? <Spinner label="正在保存" /> : "保存资料"}
                </button>
              </div>
            </form>
          </section>
          <section className="panel settings-panel" id="account-security">
            <div className="settings-heading">
              <span>
                <KeyRound size={18} />
              </span>
              <div>
                <h2>账号安全</h2>
                <p>设置仅你知道的安全口令</p>
              </div>
            </div>
            <PasswordForm api={api} initial={record} onDone={onPasswordDone} />
          </section>
        </div>
      </div>
    </>
  );
}
