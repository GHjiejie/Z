import { useCallback, useEffect, useState } from "react";
import {
  ArrowUpRight,
  ChartColumn,
  ChevronRight,
  LogOut,
  MessageSquare,
  ShieldCheck,
  UserRound,
  UsersRound,
} from "lucide-react";
import { ApiError } from "./api";
import { LoginPage, ProfilePage } from "./AccountPages";
import { RolesPage } from "./RolesPage";
import { ChatPage } from "./ChatPage";
import {
  Avatar,
  Brand,
  ErrorBanner,
  Spinner,
  Toast,
  roleLabel,
  type ToastValue,
} from "./components";
import { useSession } from "./useSession";
import { TokenUsagePage } from "./TokenUsagePage";
import { UsersPage } from "./UsersPage";

type Page = "users" | "roles" | "profile" | "chat" | "usage";

const pageTitles: Record<Page, string> = {
  users: "用户管理",
  roles: "角色与权限",
  profile: "个人设置",
  chat: "会话",
  usage: "模型用量",
};

function route(hash: string): Page {
  switch (hash.split("?")[0]) {
    case "#chat":
      return "chat";
    case "#roles":
      return "roles";
    case "#usage":
      return "usage";
    case "#profile":
      return "profile";
    default:
      return "users";
  }
}

export function App() {
  const session = useSession();
  const [hash, setHash] = useState(() => location.hash);
  const page = route(hash);
  const [toast, setToast] = useState<ToastValue | null>(null);
  const [logoutError, setLogoutError] = useState<unknown>(null);
  const [loggingOut, setLoggingOut] = useState(false);
  const notify = useCallback(
    (text: string) => setToast({ id: Date.now(), text }),
    [],
  );
  const dismiss = useCallback(() => setToast(null), []);
  const navigate = useCallback((next: Page) => {
    location.hash = next;
    setHash(location.hash);
  }, []);
  useEffect(() => {
    const changed = () => setHash(location.hash);
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const canManage = session.permissions.includes("user.read");
  const canRoles = session.permissions.includes("role.read");
  const canReadUsage = session.permissions.includes("agent.usage.read");
  useEffect(() => {
    if (session.record && !session.loading) {
      if ((page === "users" && !canManage) || (page === "roles" && !canRoles))
        navigate("profile");
    }
  }, [session.record, session.loading, page, canManage, canRoles, navigate]);

  async function logout() {
    if (loggingOut) return;
    setLoggingOut(true);
    setLogoutError(null);
    try {
      await session.api.logout();
      session.end("已退出登录");
      setToast(null);
    } catch (failure) {
      if (!(failure instanceof ApiError && failure.status === 401))
        setLogoutError(failure);
    } finally {
      setLoggingOut(false);
    }
  }
  const passwordDone = () => {
    session.end("密码已更新，请使用新密码重新登录");
    setToast(null);
  };
  if (!session.token)
    return <LoginPage notice={session.notice} onLogin={session.login} />;
  if (session.loading || (!session.record && !session.error))
    return (
      <main className="boot-screen">
        <Brand />
        <Spinner label="正在验证登录身份" />
      </main>
    );
  if (!session.record)
    return (
      <main className="boot-screen">
        <Brand />
        <div className="boot-error">
          <ErrorBanner error={session.error} onRetry={session.refresh} />
          <button className="button secondary" onClick={() => session.end("")}>
            返回登录
          </button>
        </div>
      </main>
    );
  const user = session.record.user;
  const requestedUsageUser = new URLSearchParams(hash.split("?")[1]).get(
    "user_id",
  );
  const usageUserId =
    canReadUsage &&
    requestedUsageUser &&
    /^[A-Za-z0-9_-]{1,64}$/.test(requestedUsageUser)
      ? requestedUsageUser
      : user.id;
  const accountControls = (
    <div className="topbar-account">
      <button
        className="account-button"
        onClick={() => navigate("profile")}
        aria-label="打开个人设置"
      >
        <Avatar user={user} />
        <div>
          <strong>{user.display_name}</strong>
          <small>
            {user.roles
              .map(
                (code) =>
                  session.roles.find((value) => value.code === code)
                    ?.display_name || roleLabel(code),
              )
              .join("、")}
          </small>
        </div>
      </button>
      <span className="topbar-separator" />
      <button
        className="icon-button"
        aria-label="退出登录"
        title="退出登录"
        disabled={loggingOut}
        onClick={() => {
          void logout();
        }}
      >
        <LogOut size={16} />
      </button>
    </div>
  );
  return (
    <div
      className={`console-shell${page === "chat" ? " console-shell-chat" : ""}`}
    >
      <aside className="sidebar">
        <Brand />
        <div className="sidebar-section-label">工作台</div>
        <nav aria-label="主导航">
          <a href="#chat" aria-current={page === "chat" ? "page" : undefined}>
            <MessageSquare size={18} />
            <span>会话</span>
          </a>
          <a href="#usage" aria-current={page === "usage" ? "page" : undefined}>
            <ChartColumn size={18} aria-hidden="true" />
            <span>{pageTitles.usage}</span>
          </a>
          {canManage && (
            <a
              href="#users"
              aria-current={page === "users" ? "page" : undefined}
            >
              <UsersRound size={18} />
              <span>用户管理</span>
            </a>
          )}
          {canRoles && (
            <a
              href="#roles"
              aria-current={page === "roles" ? "page" : undefined}
            >
              <ShieldCheck size={18} />
              <span>角色与权限</span>
            </a>
          )}
          <a
            href="#profile"
            aria-current={page === "profile" ? "page" : undefined}
          >
            <UserRound size={18} />
            <span>个人设置</span>
          </a>
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-role">
            <ShieldCheck size={16} />
            <div>
              <strong>{canManage || canRoles ? "管理访问" : "账号访问"}</strong>
              <small>
                {canManage || canRoles
                  ? "管理成员与系统访问权限"
                  : "管理个人资料与账号安全"}
              </small>
            </div>
          </div>
          {canManage && (
            <a
              href="/docs"
              target="_blank"
              rel="noreferrer"
              className="documentation-link"
            >
              接口文档
              <ArrowUpRight size={16} />
            </a>
          )}
        </div>
        {page === "chat" && (
          <div className="chat-sidebar-account">{accountControls}</div>
        )}
      </aside>
      <div className="workspace">
        {page !== "chat" && (
          <header className="topbar">
            <div className="breadcrumb">
              <span>Console</span>
              <ChevronRight size={16} />
              <strong>{pageTitles[page]}</strong>
            </div>
            {accountControls}
          </header>
        )}
        <main className="page-content">
          {Boolean(logoutError) && <ErrorBanner error={logoutError} />}
          {Boolean(session.error) && (
            <ErrorBanner error={session.error} onRetry={session.refresh} />
          )}
          {page === "users" && canManage && (
            <UsersPage
              api={session.api}
              currentUser={user}
              permissions={session.permissions}
              notify={notify}
              onSelfChanged={session.setRecord}
              onProfile={() => navigate("profile")}
              onUsage={(userId) => {
                location.hash = `usage?${new URLSearchParams({ user_id: userId })}`;
                setHash(location.hash);
              }}
            />
          )}
          {page === "chat" && (
            <ChatPage
              key={user.id}
              api={session.api}
              user={user}
              permissions={session.permissions}
              notify={notify}
              onSettled={session.refresh}
            />
          )}
          {page === "roles" && canRoles && (
            <RolesPage
              api={session.api}
              permissions={session.permissions}
              notify={notify}
              onPermissionsChanged={session.refresh}
            />
          )}
          {page === "usage" && (
            <TokenUsagePage
              key={`${user.id}:${usageUserId}:${canReadUsage}`}
              api={session.api}
              userId={usageUserId}
              canReadOthers={canReadUsage}
            />
          )}
          {page === "profile" && (
            <ProfilePage
              api={session.api}
              record={session.record}
              roles={session.roles}
              permissions={session.permissions}
              notify={notify}
              onChanged={session.setRecord}
              onPasswordDone={passwordDone}
            />
          )}
        </main>
        <footer className="workspace-footer">
          <span>Agent Console</span>
          <span>用户与访问权限管理</span>
        </footer>
      </div>
      <Toast value={toast} onDismiss={dismiss} />
    </div>
  );
}
