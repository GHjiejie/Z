import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Activity, ArrowRight, Bot, Boxes, Building2, CircleHelp, Command, Gauge, LayoutDashboard, LogOut, Menu, MessageSquare, ShieldCheck, SlidersHorizontal, Sparkles, Users, Wallet, X, Mail } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { api, getTenantId, resetSession, setCsrf, setTenantId, tenantLink, write } from "./api";
import type { IdentityUser, Tenant, User } from "./types";
import { Button, Empty, ErrorState, Field, Form, Loading, Modal, ToastProvider, formValue, useToast } from "./components";
import { DashboardPage, QuotasPage, AuditPage, RunsPage } from "./pages";
import { AgentManagement, AgentSidebar } from "./AgentManagement";
import type { AgentView } from "./AgentManagement";
import { Playground } from "./Playground";
import { InvitationPage, MembersPage, PlatformPage, PlatformGatewayPage, roleLabel } from "./Tenancy";
import { OwnerSupportPanel, SupportPage } from "./Support";
import { ModelStrategyPage, ResourceBillingPage, ResourceUsagePage } from "./ResourcesPages";
import { ResourcesSidebar } from "./ResourcesUI";

type Route = "overview" | "agents" | "playground" | "runs" | "models" | "usage" | "billing" | "users" | "quotas" | "audit" | "platform" | "gateway" | "invite" | "support" | "support-access";
type Navigation = { route: Route; title: string; icon: LucideIcon; group: string; capability?: string; platform?: boolean; };
const navigation: Navigation[] = [
  { route: "overview", title: "工作空间概览", icon: LayoutDashboard, group: "工作空间", capability: "runs.execute" },
  { route: "agents", title: "我的 Agent", icon: Bot, group: "工作空间", capability: "agents.read" },
  { route: "playground", title: "对话实验室", icon: MessageSquare, group: "工作空间", capability: "runs.execute" },
  { route: "runs", title: "运行记录", icon: Activity, group: "工作空间", capability: "runs.read_own" },
  { route: "models", title: "模型目录", icon: Boxes, group: "资源与费用", capability: "models.read" },
  { route: "usage", title: "调用统计", icon: Gauge, group: "资源与费用", capability: "billing.read_own" },
  { route: "billing", title: "费用中心", icon: Wallet, group: "资源与费用", capability: "billing.read" },
  { route: "users", title: "成员管理", icon: Users, group: "组织管理", capability: "members.manage" },
  { route: "quotas", title: "配额与限流", icon: SlidersHorizontal, group: "组织管理", capability: "quotas.manage" },
  { route: "audit", title: "审计日志", icon: ShieldCheck, group: "组织管理", capability: "audit.read" },
  { route: "support-access", title: "支持访问授权", icon: ShieldCheck, group: "组织管理", capability: "ownership.manage" },
  { route: "platform", title: "平台运营", icon: Building2, group: "平台管理", platform: true },
  { route: "gateway", title: "网关管理", icon: SlidersHorizontal, group: "平台管理", capability: "platform.gateway.manage", platform: true },
  { route: "support", title: "支持工作台", icon: ShieldCheck, group: "平台管理", capability: "platform.support.request", platform: true },
  { route: "invite", title: "接受组织邀请", icon: Mail, group: "账号" },
];
function getRoute(): Route {
  return navigation.find((item) => item.route === location.hash.slice(1).split("?")[0])?.route ?? "overview";
}
function selectedFromPage() {
  return new URLSearchParams(location.hash.split("?")[1] ?? "").get("tenant") ?? sessionStorage.getItem("agent-platform.tenant") ?? "";
}
export default function App() {
  return <ToastProvider>
    <Workspace />
  </ToastProvider>;
}
function Workspace() {
  const [identity, setIdentity] = useState<IdentityUser | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [tenant, setTenant] = useState<Tenant | null>(null);
  const [selected, setSelected] = useState(selectedFromPage);
  const [initializing, setInitializing] = useState(true);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextError, setContextError] = useState("");
  const [authError, setAuthError] = useState("");
  const [route, setRoute] = useState<Route>(getRoute);
  const [agentView, setAgentView] = useState<AgentView>({ mode: "list", title: "" });
  const [collapsed, setCollapsed] = useState(() => sessionStorage.getItem("agent-platform.sidebar-collapsed") === "1");
  const agentViewChanged = useCallback((view: AgentView) => setAgentView(view), []);
  const [mobile, setMobile] = useState(false);
  const [password, setPassword] = useState(false);
  const [contextReload, setContextReload] = useState(0);
  const toast = useToast();
  const identityRequest = useRef<AbortController | null>(null);
  const identityId = useRef<string | null>(null);
  const clearSession = useCallback(() => {
    identityRequest.current?.abort();
    identityRequest.current = null;
    identityId.current = null;
    resetSession(); setIdentity(null); setUser(null); setTenant(null); setPassword(false); setInitializing(false);
  }, []);
  const refreshIdentity = useCallback(async () => {
    identityRequest.current?.abort();
    const controller = new AbortController();
    identityRequest.current = controller;
    try {
      const result = await api<{ user: IdentityUser; csrf_token: string }>("/api/v2/me", { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (identityId.current && identityId.current !== result.user.id) {
        resetSession(); setUser(null); setTenant(null);
      }
      identityId.current = result.user.id;
      setIdentity(result.user); setCsrf(result.csrf_token); setAuthError("");
    } catch (error) {
      if (controller.signal.aborted || (error as { code?: string }).code === "scope_changed") return;
      if ((error as { status?: number }).status === 401) clearSession();
      else setAuthError((error as Error).message);
    } finally { if (identityRequest.current === controller) { identityRequest.current = null; setInitializing(false); } }
  }, [clearSession]);
  useEffect(() => {
    void refreshIdentity();
    const change = () => {
      const next = selectedFromPage();
      if (next !== getTenantId()) { setTenantId(next); setUser(null); setTenant(null); }
      setRoute(getRoute()); setSelected(next); setMobile(false);
    };
    const expired = () => { clearSession(); setInitializing(false); setAuthError("登录已过期，请重新登录。"); };
    const refresh = () => { void refreshIdentity(); };
    const accessChanged = () => { setContextReload((value) => value + 1); void refreshIdentity(); };
    window.addEventListener("hashchange", change);
    window.addEventListener("auth-expired", expired);
    window.addEventListener("focus", refresh);
    window.addEventListener("tenant-access-changed", accessChanged);
    const timer = setInterval(refresh, 15000);
    return () => { identityRequest.current?.abort(); window.removeEventListener("hashchange", change); window.removeEventListener("auth-expired", expired); window.removeEventListener("focus", refresh); window.removeEventListener("tenant-access-changed", accessChanged); clearInterval(timer); };
  }, [refreshIdentity, clearSession]);
  const membershipKey = JSON.stringify(identity?.memberships ?? []);
  const platformKey = JSON.stringify(identity?.capabilities ?? []);
  useEffect(() => {
    if (!identity) return;
    const memberships = identity.memberships.filter((item) => item.status === "active");
    let next = selected;
    if (!memberships.some((item) => item.tenant_id === next)) next = memberships.find((item) => item.tenant_status === "active")?.tenant_id ?? memberships[0]?.tenant_id ?? "";
    if (next !== selected) {
      setSelected(next);
      const query = new URLSearchParams(location.hash.split("?")[1] ?? "");
      if (query.has("tenant")) { query.delete("session"); query.delete("agent"); if (next) query.set("tenant", next); else query.delete("tenant"); history.replaceState(null, "", `#${getRoute()}${query.size ? `?${query}` : ""}`); }
    }
    setTenantId(next);
    if (next) sessionStorage.setItem("agent-platform.tenant", next); else sessionStorage.removeItem("agent-platform.tenant");
  }, [membershipKey, selected, identity?.id]);
  useEffect(() => {
    if (!identity || !selected || !identity.memberships.some((item) => item.tenant_id === selected && item.status === "active")) { setUser(null); setTenant(null); return; }
    let disposed = false;
    setContextLoading(true); setContextError("");
    api<{ tenant: Tenant; user: User }>(`/api/v2/tenants/${encodeURIComponent(selected)}`)
      .then((result) => { if (!disposed) { setUser(result.user); setTenant(result.tenant); } })
      .catch((error) => { if (!disposed) { setContextError(error.message); setUser(null); setTenant(null); } })
      .finally(() => { if (!disposed) setContextLoading(false); });
    return () => { disposed = true; };
  }, [selected, membershipKey, platformKey, identity?.id, contextReload]);
  const navigate = (next: string) => { location.hash = tenantLink(next); };
  const chooseTenant = (id: string) => {
    setTenantId(id); setSelected(id); setUser(null); setTenant(null);
    sessionStorage.setItem("agent-platform.tenant", id);
    location.hash = tenantLink("overview", id);
  };
  async function logout() {
    try { await write("/auth/logout"); clearSession(); } catch (error) { toast((error as Error).message, "error"); }
  }
  if (initializing) return <div className="boot">
    <div className="brand-symbol">
      <Command size={28} />
    </div>
    <Loading />
  </div>;
  if (!identity) return <Login error={authError} loggedIn={(_value, token) => { clearSession(); setCsrf(token); void refreshIdentity(); }} />;
  const platformCapabilities = identity.capabilities ?? [];
  const hasPlatform = platformCapabilities.some((capability) => ["platform.tenants.manage", "platform.billing.manage", "platform.entitlements.manage", "platform.models.manage"].includes(capability));
  const readOnlyTenant = tenant != null && tenant.status !== "active";
  const visible = navigation.filter((item) => {
    if (item.platform) return item.capability ? platformCapabilities.includes(item.capability) : hasPlatform;
    if (item.route === "invite") return true;
    if (!user || user.tenant_id !== selected) return false;
    if (readOnlyTenant && !["usage", "billing", "models", "audit", "support-access"].includes(item.route)) return false;
    return !item.capability || user.capabilities?.includes(item.capability) || item.route === "usage" && user.capabilities?.includes("usage.read_all");
  });
  const allowedRoute = visible.some((item) => item.route === route);
  const safeRoute: Route = allowedRoute ? route : visible.find((item) => !item.platform && item.route !== "invite")?.route ?? (hasPlatform ? "platform" : "invite");
  const current = navigation.find((item) => item.route === safeRoute)!;
  const isGlobal = current.platform || safeRoute === "invite";
  const identityName = identity.name || identity.email;
  const memberships = identity.memberships.filter((item) => item.status === "active");
  const currentMembership = memberships.find((item) => item.tenant_id === selected);
  const canManageAgents = user?.capabilities?.includes("agents.manage") ?? false;
  const agentRoute = safeRoute === "agents";
  const resourceRoute = ["models", "billing", "usage"].includes(safeRoute);
  const resourceTitle = safeRoute === "models" ? "模型策略" : safeRoute === "billing" ? "费用中心" : "调用统计与导出";
  const toggleSidebar = () => { setCollapsed(value => { sessionStorage.setItem("agent-platform.sidebar-collapsed", value ? "0" : "1"); return !value; }); };
  return <div className={`app-shell ${agentRoute ? `agent-shell ${agentView.mode === "list" ? "agent-list-shell" : ""} ${collapsed ? "agent-collapsed" : ""}` : resourceRoute ? `resources-shell resources-${safeRoute}-shell ${collapsed ? "resources-collapsed" : ""}` : ""}`}>
    {mobile && <button className="sidebar-backdrop" aria-label="关闭导航" onClick={() => setMobile(false)} />}
    <aside className={`sidebar ${mobile ? "open" : ""}`}>
      {resourceRoute ? <ResourcesSidebar items={visible} route={safeRoute} collapsed={collapsed && !mobile} toggle={toggleSidebar} /> : agentRoute ? <AgentSidebar items={visible} collapsed={collapsed && !mobile} toggle={toggleSidebar} list={agentView.mode === "list"} /> : <>
      <a className="brand" href={tenantLink("overview")}>
        <span className="brand-symbol">
          <Command size={22} />
        </span>
        <span>Agent<span className="brand-light"> Platform</span>
          <small>BUILD. RUN. UNDERSTAND.</small>
        </span>
      </a>
      <div className="workspace-switch tenant-switch">
        <div className="workspace-icon">
          <Building2 size={20} />
        </div>
        <label>
          <span>当前组织</span>
          <select aria-label="切换组织" value={selected} onChange={(event) => chooseTenant(event.target.value)} disabled={!memberships.length}>
            {!memberships.length && <option value="">尚未加入组织</option>}
            {memberships.map((item) => <option key={item.tenant_id} value={item.tenant_id}>
              {item.tenant_name ?? item.tenant_id}{item.tenant_status !== "active" ? "（已暂停）" : ""}</option>)}
          </select>
          <small>
            {currentMembership ? roleLabel(currentMembership.role) : "个人账号"}</small>
        </label>
      </div>
      <nav aria-label="主导航">
        {["工作空间", "资源与费用", "组织管理", "平台管理", "账号"].map((group) => {
          const items = visible.filter((item) => item.group === group);
          return items.length ? <div className="nav-group" key={group}>
            <div className="nav-label">
              {group}</div>
            {items.map((item) => <a key={item.route} href={tenantLink(item.route)} className={`nav-link ${safeRoute === item.route ? "selected" : ""}`} aria-current={safeRoute === item.route ? "page" : undefined}>
              <item.icon size={18} strokeWidth={1.8} />
              {item.title}{safeRoute === item.route && <span className="nav-active-dot" />}</a>)}</div> : null;
        })}</nav>
      <div className="sidebar-bottom">
        <div className="workspace-note">
          <ShieldCheck size={16} />
          <span>每个组织独立管理<br />成员、Agent 与使用额度。</span>
        </div>
        <button className="profile" onClick={() => setPassword(true)} title="账号与密码">
          <span className="avatar">
            {identityName.slice(0, 1).toUpperCase()}</span>
          <span>
            <strong>
              {identityName}</strong>
            <small>
              {hasPlatform ? "平台运营账号" : "我的账号"}</small>
          </span>
          <SlidersHorizontal size={16} />
        </button>
      </div>
      </>}
    </aside>
    <main className="main-shell">
      <div className="topbar">
        <div className="breadcrumb">
          <button className="icon-button mobile-toggle" aria-label="打开导航" onClick={() => setMobile(true)}>
            <Menu size={21} />
          </button>
          <span>
            {resourceRoute ? safeRoute === "usage" ? "工作空间" : "资源与费用" : isGlobal ? "账号与平台" : tenant?.name ?? currentMembership?.tenant_name ?? "工作空间"}</span>
          <span className="breadcrumb-slash">/</span>
          <strong>
            {resourceRoute ? resourceTitle : agentRoute ? "Agent管理" : current.title}</strong>
          {agentRoute && agentView.mode !== "list" && <><span className="breadcrumb-slash">/</span><strong>{agentView.title}</strong></>}
        </div>
        <div className="topbar-right">
          {agentRoute || resourceRoute ? <details className="agent-context-menu"><summary>{tenant?.name ?? "工作空间"}</summary><div className="agent-context-popover">
            <label>当前组织<select aria-label="切换组织" value={selected} onChange={event => chooseTenant(event.target.value)}>{memberships.map(item => <option key={item.tenant_id} value={item.tenant_id}>{item.tenant_name ?? item.tenant_id}{item.tenant_status !== "active" ? "（已暂停）" : ""}</option>)}</select></label>
            <small>{currentMembership ? roleLabel(currentMembership.role) : "个人账号"} · {identityName}</small>
            <button onClick={() => setPassword(true)}>账号与密码</button>
            {visible.filter(item => item.platform || item.route === "invite").map(item => <a key={item.route} href={tenantLink(item.route)}>{item.title}</a>)}
            <button onClick={() => void logout()}>退出登录</button>
          </div></details> : <>
          <span className="edition"><span />多租户工作空间</span>
          <button className="icon-button" aria-label="账号设置" onClick={() => setPassword(true)}><CircleHelp size={19} /></button>
          <button className="icon-button" aria-label="退出登录" onClick={() => void logout()}><LogOut size={18} /></button>
          </>}

        </div>
      </div>
      <div className={`page-content ${safeRoute === "playground" ? "playground-page" : ""}`} key={`${identity.id}:${selected}:${safeRoute}:${JSON.stringify(user?.capabilities)}:${platformKey}`}>
        {contextError && isGlobal && route !== "platform" && route !== "invite" && <div className="form-error" role="alert">
          {contextError}</div>}
        {readOnlyTenant && !isGlobal && <div className="info-banner">
          <ShieldCheck size={20} />
          <div>
            <strong>组织已暂停运行</strong>
            <p>你仍可按权限查看账单与必要记录；新任务和配置修改暂不可用。</p>
          </div>
        </div>}
        {authError && <div className="form-error" role="alert">
          {authError}</div>}
        {safeRoute === "platform" && <PlatformPage identity={identity} changed={refreshIdentity} />}
        {safeRoute === "gateway" && <PlatformGatewayPage />}
        {safeRoute === "support" && <SupportPage />}
        {safeRoute === "invite" && <InvitationPage identity={identity} accepted={refreshIdentity} />}
        {!isGlobal && (contextLoading ? <Loading /> : contextError ? <ErrorState message={contextError} retry={() => setContextReload((value) => value + 1)} /> : !user || user.tenant_id !== selected ? <Empty title="选择一个组织" description="加入组织后即可使用组织授权的 Agent 和模型。" /> : tenant && !["active", "suspended", "closing"].includes(tenant.status) ? <Empty title="该组织当前不可用" description="组织已暂停或正在开通，请联系平台运营人员，或切换到其他组织。" /> : <>
          {safeRoute === "overview" && <DashboardPage user={user} navigate={navigate} />}
          {safeRoute === "agents" && <AgentManagement admin={canManageAgents} canRun={user.capabilities?.includes("runs.execute") ?? false} navigate={navigate} onViewChange={agentViewChanged} />}
          {safeRoute === "playground" && <Playground />}
          {safeRoute === "runs" && <RunsPage navigate={navigate} />}
          {safeRoute === "models" && <ModelStrategyPage canPolicy={!readOnlyTenant && (user.capabilities?.includes("models.policy") ?? false)} />}
          {safeRoute === "users" && <MembersPage user={user} changed={refreshIdentity} />}
          {safeRoute === "support-access" && <OwnerSupportPanel user={user} />}
          {safeRoute === "usage" && <ResourceUsagePage user={user} navigate={navigate} />}
          {safeRoute === "billing" && <ResourceBillingPage />}
          {safeRoute === "quotas" && <QuotasPage tenantId={user.tenant_id} />}
          {safeRoute === "audit" && <AuditPage />}
        </>)}
      </div>
      <footer className="footer">
        <span>Agent Platform</span>
        <span>金额以 USD 计价 · 时间按设备时区显示</span>
      </footer>
    </main>
    {password && <Modal title="账号与密码" description={`${identityName} · ${identity.email}`} close={() => setPassword(false)}>
      <Form close={() => setPassword(false)} label="更新密码" submit={async (form) => { await write("/auth/password", { current_password: formValue(form, "current_password"), new_password: formValue(form, "new_password") }); toast("密码已更新，请重新登录"); clearSession(); }}>
        <Field label="当前密码">
          <input name="current_password" type="password" autoComplete="current-password" required />
        </Field>
        <Field label="新密码" hint="至少 12 个字符，修改后需要重新登录。">
          <input name="new_password" type="password" autoComplete="new-password" minLength={12} required />
        </Field>
      </Form>
    </Modal>}
  </div>;
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
