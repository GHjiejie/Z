import { useEffect, useState } from "react";
import type { FormEvent } from "react";
import {
  Activity,
  ArrowRight,
  Bot,
  Boxes,
  CircleHelp,
  Command,
  Gauge,
  LayoutDashboard,
  LogOut,
  Menu,
  MessageSquare,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Users,
  Wallet,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api, setCsrf, write } from "./api";
import type { User } from "./types";
import {
  Button,
  Field,
  Form,
  Loading,
  Modal,
  ToastProvider,
  formValue,
  useToast,
} from "./components";
import {
  DashboardPage,
  AgentsPage,
  ModelsPage,
  GatewayPage,
  UsersPage,
  UsagePage,
  BillingPage,
  QuotasPage,
  AuditPage,
  RunsPage,
} from "./pages";
import { Playground } from "./Playground";

type Route =
  | "overview"
  | "agents"
  | "playground"
  | "runs"
  | "models"
  | "gateway"
  | "usage"
  | "billing"
  | "users"
  | "quotas"
  | "audit";
const navigation: {
  route: Route;
  title: string;
  icon: LucideIcon;
  admin?: boolean;
  group: string;
}[] = [
  {
    route: "overview",
    title: "工作空间概览",
    icon: LayoutDashboard,
    group: "工作空间",
  },
  { route: "agents", title: "我的 Agent", icon: Bot, group: "工作空间" },
  {
    route: "playground",
    title: "对话实验室",
    icon: MessageSquare,
    group: "工作空间",
  },
  { route: "runs", title: "运行记录", icon: Activity, group: "工作空间" },
  { route: "models", title: "模型目录", icon: Boxes, group: "资源与费用" },
  { route: "usage", title: "调用统计", icon: Gauge, group: "资源与费用" },
  { route: "billing", title: "费用中心", icon: Wallet, group: "资源与费用" },
  {
    route: "gateway",
    title: "LiteLLM 网关",
    icon: SlidersHorizontal,
    admin: true,
    group: "组织管理",
  },
  {
    route: "users",
    title: "成员管理",
    icon: Users,
    admin: true,
    group: "组织管理",
  },
  {
    route: "quotas",
    title: "配额与限流",
    icon: SlidersHorizontal,
    admin: true,
    group: "组织管理",
  },
  {
    route: "audit",
    title: "审计日志",
    icon: ShieldCheck,
    admin: true,
    group: "组织管理",
  },
];
function getRoute(): Route {
  return (
    navigation.find(
      (item) => item.route === location.hash.slice(1).split("?")[0],
    )?.route ?? "overview"
  );
}
export default function App() {
  return (
    <ToastProvider>
      <Workspace />
    </ToastProvider>
  );
}
function Workspace() {
  const [user, setUser] = useState<User | null>(null);
  const [initializing, setInitializing] = useState(true);
  const [authError, setAuthError] = useState("");
  const [route, setRoute] = useState<Route>(getRoute());
  const [mobile, setMobile] = useState(false);
  const [password, setPassword] = useState(false);
  const toast = useToast();
  useEffect(() => {
    api<{ user: User; csrf_token: string }>("/auth/me")
      .then((result) => {
        setUser(result.user);
        setCsrf(result.csrf_token);
      })
      .catch((error) => {
        if (error.status !== 401) setAuthError(error.message);
      })
      .finally(() => setInitializing(false));
    const change = () => {
      setRoute(getRoute());
      setMobile(false);
    };
    const expired = () => {
      setUser(null);
      setCsrf("");
      setAuthError("登录已过期，请重新登录。");
    };
    window.addEventListener("hashchange", change);
    window.addEventListener("auth-expired", expired);
    return () => {
      window.removeEventListener("hashchange", change);
      window.removeEventListener("auth-expired", expired);
    };
  }, []);
  const navigate = (next: string) => {
    location.hash = next;
  };
  if (initializing)
    return (
      <div className="boot">
        <div className="brand-symbol">
          <Command size={28} />
        </div>
        <Loading />
      </div>
    );
  if (!user)
    return (
      <Login
        error={authError}
        loggedIn={(value, token) => {
          setUser(value);
          setCsrf(token);
          setAuthError("");
        }}
      />
    );
  const admin = user.role === "admin";
  const current = navigation.find((item) => item.route === route)!;
  const safeRoute = current.admin && !admin ? "overview" : route;
  async function logout() {
    try {
      await write("/auth/logout");
      setUser(null);
      setCsrf("");
    } catch (error) {
      toast((error as Error).message, "error");
    }
  }
  return (
    <div className="app-shell">
      {mobile && (
        <button
          className="sidebar-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobile(false)}
        />
      )}
      <aside className={`sidebar ${mobile ? "open" : ""}`}>
        <a className="brand" href="#overview">
          <span className="brand-symbol">
            <Command size={22} />
          </span>
          <span>
            Agent<span className="brand-light"> Platform</span>
            <small>BUILD. RUN. UNDERSTAND.</small>
          </span>
        </a>
        <div className="workspace-switch">
          <div className="workspace-icon">W</div>
          <div>
            <strong>团队工作空间</strong>
            <span>{admin ? "管理员控制台" : "成员控制台"}</span>
          </div>
          <span className="workspace-dot" />
        </div>
        <nav aria-label="主导航">
          {["工作空间", "资源与费用", "组织管理"].map((group) => {
            const items = navigation.filter(
              (item) => item.group === group && (!item.admin || admin),
            );
            return items.length ? (
              <div className="nav-group" key={group}>
                <div className="nav-label">{group}</div>
                {items.map((item) => (
                  <a
                    key={item.route}
                    href={`#${item.route}`}
                    className={`nav-link ${safeRoute === item.route ? "selected" : ""}`}
                    aria-current={safeRoute === item.route ? "page" : undefined}
                  >
                    <item.icon size={18} strokeWidth={1.8} />
                    {item.title}
                    {safeRoute === item.route && (
                      <span className="nav-active-dot" />
                    )}
                  </a>
                ))}
              </div>
            ) : null;
          })}
        </nav>
        <div className="sidebar-bottom">
          <div className="workspace-note">
            <Sparkles size={16} />
            <span>
              让每一次智能调用
              <br />
              都清晰、可控。
            </span>
          </div>
          <button
            className="profile"
            onClick={() => setPassword(true)}
            title="账号与密码"
          >
            <span className="avatar">
              {(user.name || user.email).slice(0, 1).toUpperCase()}
            </span>
            <span>
              <strong>{user.name || user.email}</strong>
              <small>{admin ? "管理员" : "成员"}</small>
            </span>
            <SlidersHorizontal size={16} />
          </button>
        </div>
      </aside>
      <main className="main-shell">
        <div className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-toggle"
              aria-label="打开导航"
              onClick={() => setMobile(true)}
            >
              <Menu size={21} />
            </button>
            <span>工作空间</span>
            <span className="breadcrumb-slash">/</span>
            <strong>
              {navigation.find((item) => item.route === safeRoute)?.title}
            </strong>
          </div>
          <div className="topbar-right">
            <span className="edition">
              <span />
              自部署 · v0.1
            </span>
            <button
              className="icon-button"
              title="账号设置"
              aria-label="账号设置"
              onClick={() => setPassword(true)}
            >
              <CircleHelp size={19} />
            </button>
            <button
              className="icon-button"
              title="退出登录"
              aria-label="退出登录"
              onClick={() => void logout()}
            >
              <LogOut size={18} />
            </button>
          </div>
        </div>
        <div
          className={`page-content ${safeRoute === "playground" ? "playground-page" : ""}`}
          key={safeRoute}
        >
          {safeRoute === "overview" && (
            <DashboardPage user={user} navigate={navigate} />
          )}
          {safeRoute === "agents" && (
            <AgentsPage admin={admin} navigate={navigate} />
          )}
          {safeRoute === "playground" && <Playground />}
          {safeRoute === "runs" && <RunsPage navigate={navigate} />}
          {safeRoute === "models" && <ModelsPage admin={admin} />}
          {safeRoute === "gateway" && <GatewayPage />}
          {safeRoute === "users" && <UsersPage currentUser={user} />}
          {safeRoute === "usage" && <UsagePage />}
          {safeRoute === "billing" && <BillingPage admin={admin} />}
          {safeRoute === "quotas" && <QuotasPage tenantId={user.tenant_id} />}
          {safeRoute === "audit" && <AuditPage />}
        </div>
        <footer className="footer">
          <span>Agent Platform</span>
          <span>所有金额以 USD 计价 · 时间按设备时区显示</span>
        </footer>
      </main>
      {password && (
        <Modal
          title="账号与密码"
          description={`${user.name} · ${user.email}`}
          close={() => setPassword(false)}
        >
          <Form
            close={() => setPassword(false)}
            label="更新密码"
            submit={async (form) => {
              await write("/auth/password", {
                current_password: formValue(form, "current_password"),
                new_password: formValue(form, "new_password"),
              });
              toast("密码已更新，请重新登录");
              setPassword(false);
              setUser(null);
              setCsrf("");
            }}
          >
            <Field label="当前密码">
              <input
                name="current_password"
                type="password"
                autoComplete="current-password"
                required
              />
            </Field>
            <Field
              label="新密码"
              hint="至少 12 个字符，建议使用独立且难以猜测的密码。"
            >
              <input
                name="new_password"
                type="password"
                autoComplete="new-password"
                minLength={12}
                required
              />
            </Field>
          </Form>
        </Modal>
      )}
    </div>
  );
}
function Login({
  error: initialError,
  loggedIn,
}: {
  error: string;
  loggedIn: (user: User, token: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    setBusy(true);
    setError("");
    try {
      const result = await write<{ user: User; csrf_token: string }>(
        "/auth/login",
        {
          email: formValue(form, "email"),
          password: formValue(form, "password"),
        },
      );
      loggedIn(result.user, result.csrf_token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <section className="login-story">
        <a href="#" className="brand">
          <span className="brand-symbol">
            <Command size={25} />
          </span>
          <span>Agent Platform</span>
        </a>
        <div className="story-copy">
          <div className="story-tag">
            <span />
            YOUR AI WORKSPACE
          </div>
          <h1>
            让想法运行。
            <br />
            <span>让智能可控。</span>
          </h1>
          <p>
            从第一个 Agent 到团队的智能工作空间。
            <br />
            统一模型、连接工具，让每次调用都有迹可循。
          </p>
          <div className="story-diagram">
            <div className="diagram-agent">
              <Bot size={28} />
              <span>你的 Agent</span>
            </div>
            <div className="diagram-connector">
              <span />
              <span />
              <span />
            </div>
            <div className="diagram-stack">
              <div>
                <Boxes size={18} />
                模型网关
              </div>
              <div>
                <ShieldCheck size={18} />
                配额与权限
              </div>
              <div>
                <Activity size={18} />
                用量与费用
              </div>
            </div>
          </div>
        </div>
        <div className="story-footer">
          PYTHON + REACT + LITELLM<span>ONE CONNECTED PLATFORM</span>
        </div>
      </section>
      <section className="login-form-section">
        <div className="login-form">
          <div className="login-greeting">
            <span className="login-spark">
              <Sparkles size={24} />
            </span>
            <h2>欢迎回来</h2>
            <p>登录以进入你的 Agent 工作空间</p>
          </div>
          <form onSubmit={submit}>
            <Field label="工作邮箱">
              <input
                type="email"
                name="email"
                autoComplete="username"
                placeholder="you@company.com"
                required
                autoFocus
              />
            </Field>
            <Field label="密码">
              <input
                type="password"
                name="password"
                autoComplete="current-password"
                placeholder="输入你的登录密码"
                required
              />
            </Field>
            {error && (
              <div className="form-error" role="alert">
                {error}
                <button
                  type="button"
                  onClick={() => setError("")}
                  aria-label="关闭错误"
                >
                  <X size={14} />
                </button>
              </div>
            )}
            <Button type="submit" busy={busy}>
              进入工作空间
              <ArrowRight size={18} />
            </Button>
          </form>
          <p className="login-help">
            账号由工作空间管理员创建。
            <br />
            首次部署请按项目说明初始化管理员账号。
          </p>
        </div>
        <span className="login-foot">让智能发生，让成本透明。</span>
      </section>
    </div>
  );
}
