import { useEffect, useState } from "react";
import {
  ArrowLeftRight,
  ChevronLeft,
  ChevronRight,
  ListFilter,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  UserCheck,
  UsersRound,
  X,
} from "lucide-react";
import {
  aborted,
  ApiClient,
  type User,
  type UserPage,
  type UserQuery,
  type UserRecord,
} from "./api";
import {
  ActionMenu,
  Avatar,
  EmptyState,
  ErrorBanner,
  formatDate,
  RoleBadge,
  Spinner,
  StatusBadge,
  type MenuAction,
} from "./components";
import { UserDialog, type UserDialogState } from "./UserDialog";

const defaults: UserQuery = {
  page: 1,
  page_size: 10,
  q: "",
  status: "",
  role: "",
  sort_by: "created_at",
  sort_order: "desc",
};

export function UsersPage({
  api,
  currentUser,
  permissions,
  notify,
  onSelfChanged,
  onProfile,
}: {
  api: ApiClient;
  currentUser: User;
  permissions: string[];
  notify: (message: string) => void;
  onSelfChanged: (record: UserRecord) => void;
  onProfile: () => void;
}) {
  const [query, setQuery] = useState<UserQuery>(defaults);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState<UserPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);
  const [revision, setRevision] = useState(0);
  const [stats, setStats] = useState<{
    total: number;
    active: number;
    admins: number;
  } | null>(null);
  const [statsError, setStatsError] = useState(false);
  const [dialog, setDialog] = useState<UserDialogState | null>(null);
  const refresh = () => setRevision((value) => value + 1);
  const can = (action: string) => permissions.includes(action);
  useEffect(() => {
    const timer = window.setTimeout(
      () =>
        setQuery((value) =>
          value.q === search.trim()
            ? value
            : { ...value, q: search.trim(), page: 1 },
        ),
      300,
    );
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    api
      .users(query, controller.signal)
      .then((data) => {
        if (controller.signal.aborted) return;
        if (query.page > 1 && !data.items.length && data.total > 0) {
          setQuery((value) => ({
            ...value,
            page: Math.ceil(data.total / value.page_size),
          }));
          return;
        }
        if (query.page > 1 && data.total === 0) {
          setQuery((value) => ({ ...value, page: 1 }));
          return;
        }
        setPage(data);
        setLoading(false);
      })
      .catch((failure) => {
        if (!aborted(failure) && !controller.signal.aborted) {
          setError(failure);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, query, revision]);
  useEffect(() => {
    const controller = new AbortController();
    setStatsError(false);
    Promise.all([
      api.users({ page: 1, page_size: 1 }, controller.signal),
      api.users({ page: 1, page_size: 1, status: "active" }, controller.signal),
      api.users({ page: 1, page_size: 1, role: "admin" }, controller.signal),
    ])
      .then(([all, active, admin]) => {
        if (!controller.signal.aborted)
          setStats({
            total: all.total,
            active: active.total,
            admins: admin.total,
          });
      })
      .catch((failure) => {
        if (!aborted(failure) && !controller.signal.aborted)
          setStatsError(true);
      });
    return () => controller.abort();
  }, [api, revision]);

  function saved(message: string, record?: UserRecord) {
    setDialog(null);
    refresh();
    notify(message);
    if (record?.user.id === currentUser.id) onSelfChanged(record);
  }
  function actions(user: User): MenuAction[] {
    const self = user.id === currentUser.id;
    const items: MenuAction[] = [];
    if (can("user.roles.assign"))
      items.push({
        label: "设置角色",
        onClick: () => setDialog({ kind: "roles", userId: user.id }),
        disabled: self,
        reason: self ? "请由其他管理员调整你的角色" : undefined,
      });
    if (self) items.push({ label: "修改我的密码", onClick: onProfile });
    else if (can("user.password.reset"))
      items.push({
        label: "重置密码",
        onClick: () => setDialog({ kind: "reset", userId: user.id }),
      });
    if (can("user.status.update"))
      items.push({
        label: user.status === "active" ? "禁用账号" : "启用账号",
        disabled: self,
        reason: self ? "不能禁用当前账号" : undefined,
        onClick: () =>
          setDialog({
            kind: "status",
            userId: user.id,
            nextStatus: user.status === "active" ? "disabled" : "active",
          }),
      });
    if (can("user.delete"))
      items.push({
        label: "删除用户",
        danger: true,
        disabled: self,
        reason: self ? "不能删除当前账号" : undefined,
        onClick: () => setDialog({ kind: "delete", userId: user.id }),
      });
    return items;
  }
  const filtered = Boolean(query.q || query.status || query.role);
  const pageCount = page
    ? Math.max(1, Math.ceil(page.total / query.page_size))
    : 1;
  return (
    <>
      <header className="page-heading">
        <div>
          <div className="eyebrow">成员与访问</div>
          <h1>用户管理</h1>
          <p>管理系统成员、访问角色和账号状态。</p>
        </div>
        {can("user.create") && (
          <button
            className="button primary"
            onClick={() => setDialog({ kind: "create" })}
          >
            <Plus size={17} />
            新增用户
          </button>
        )}
      </header>
      <div className="stats-grid">
        {[
          {
            label: "系统用户",
            value: stats?.total,
            hint: "当前系统中的全部用户",
            icon: UsersRound,
            tone: "blue",
          },
          {
            label: "已启用账号",
            value: stats?.active,
            hint: "可以登录并访问系统",
            icon: UserCheck,
            tone: "green",
          },
          {
            label: "管理员",
            value: stats?.admins,
            hint: "已分配管理员角色的用户",
            icon: ShieldCheck,
            tone: "indigo",
          },
        ].map((item) => (
          <section className="stat-card" key={item.label}>
            <div>
              <span className="stat-label">{item.label}</span>
              <strong>{statsError ? "—" : (item.value ?? "—")}</strong>
              <small>
                {statsError ? "暂时无法更新，点击刷新重试" : item.hint}
              </small>
            </div>
            <span className={`stat-icon ${item.tone}`}>
              <item.icon size={21} />
            </span>
          </section>
        ))}
      </div>
      <section className="panel users-panel" aria-label="用户列表">
        <div className="list-heading">
          <div>
            <h2>
              全部用户{" "}
              <span className="count-badge">{stats?.total ?? "—"}</span>
            </h2>
            <p>维护成员信息，并为每位用户分配合适的权限。</p>
          </div>
          <button
            type="button"
            className="button secondary compact"
            disabled={loading}
            onClick={refresh}
          >
            <RefreshCw size={15} className={loading ? "spin" : ""} />
            刷新
          </button>
        </div>
        <div className="filters">
          <div className="search-control">
            <Search size={17} aria-hidden="true" />
            <input
              aria-label="搜索用户"
              placeholder="搜索用户名、名称或邮箱"
              maxLength={100}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {search && (
              <button
                className="icon-button"
                aria-label="清空搜索"
                onClick={() => setSearch("")}
              >
                <X size={14} />
              </button>
            )}
          </div>
          <div className="filter-control">
            <ListFilter size={15} aria-hidden="true" />
            <select
              aria-label="角色筛选"
              value={query.role}
              onChange={(event) => {
                const value = event.target.value;
                if (value === "" || value === "admin" || value === "member")
                  setQuery((previous) => ({
                    ...previous,
                    page: 1,
                    role: value,
                  }));
              }}
            >
              <option value="">全部角色</option>
              <option value="admin">管理员</option>
              <option value="member">普通用户</option>
            </select>
          </div>
          <select
            className="select-control"
            aria-label="状态筛选"
            value={query.status}
            onChange={(event) => {
              const value = event.target.value;
              if (value === "" || value === "active" || value === "disabled")
                setQuery((previous) => ({
                  ...previous,
                  page: 1,
                  status: value,
                }));
            }}
          >
            <option value="">全部状态</option>
            <option value="active">已启用</option>
            <option value="disabled">已禁用</option>
          </select>
          <select
            className="select-control sort-control"
            aria-label="排序方式"
            value={query.sort_by}
            onChange={(event) => {
              const value = event.target.value;
              if (
                value === "created_at" ||
                value === "username" ||
                value === "last_login_at"
              )
                setQuery((previous) => ({
                  ...previous,
                  page: 1,
                  sort_by: value,
                  sort_order: value === "username" ? "asc" : "desc",
                }));
            }}
          >
            <option value="created_at">最近创建</option>
            <option value="username">用户名 A–Z</option>
            <option value="last_login_at">最近登录</option>
          </select>
        </div>
        <p className="table-mobile-hint">
          <ArrowLeftRight size={12} />
          左右滑动查看完整列表与操作
        </p>
        {Boolean(error) && (
          <div className="list-error">
            <ErrorBanner error={error} onRetry={refresh} />
            {page && <small>当前保留上次成功加载的结果。</small>}
          </div>
        )}
        {loading && !page ? (
          <div className="table-loading">
            <Spinner label="正在加载用户列表" />
            <div className="skeleton-lines">
              {Array.from({ length: 5 }, (_, index) => (
                <div key={index} />
              ))}
            </div>
          </div>
        ) : page?.items.length ? (
          <div
            className={`table-scroll${loading ? " is-refreshing" : ""}`}
            aria-busy={loading}
          >
            <table>
              <thead>
                <tr>
                  <th>用户</th>
                  <th>角色</th>
                  <th>状态</th>
                  <th>最近登录</th>
                  <th>创建时间</th>
                  <th className="actions-cell">操作</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((user) => (
                  <tr key={user.id}>
                    <td>
                      <div className="user-cell">
                        <Avatar user={user} />
                        <div>
                          <div className="user-title">
                            <strong title={user.display_name}>
                              {user.display_name}
                            </strong>
                            {user.id === currentUser.id && (
                              <span className="self-label">你</span>
                            )}
                          </div>
                          <span
                            className="user-secondary"
                            title={`${user.username}${user.email ? ` · ${user.email}` : ""}`}
                          >
                            {user.username}
                            {user.email && (
                              <>
                                <span className="muted-dot">·</span>
                                {user.email}
                              </>
                            )}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <RoleBadge admin={user.roles.includes("admin")} />
                    </td>
                    <td>
                      <StatusBadge status={user.status} />
                    </td>
                    <td
                      className={
                        user.last_login_at ? "date-cell" : "muted-cell"
                      }
                    >
                      {formatDate(user.last_login_at)}
                    </td>
                    <td className="date-cell">
                      {formatDate(user.created_at, true)}
                    </td>
                    <td>
                      <div className="row-actions">
                        {can("user.update") && (
                          <button
                            className="text-button"
                            aria-label={`编辑用户：${user.username}`}
                            onClick={() =>
                              setDialog({ kind: "edit", userId: user.id })
                            }
                          >
                            编辑
                          </button>
                        )}
                        <ActionMenu user={user} actions={actions(user)} />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          !error && (
            <EmptyState
              title={filtered ? "没有找到符合条件的用户" : "还没有用户"}
              description={
                filtered
                  ? "试试其他关键词，或调整角色和状态筛选。"
                  : "添加第一位成员，开始管理访问权限。"
              }
              action={
                filtered && (
                  <button
                    className="button secondary"
                    onClick={() => {
                      setSearch("");
                      setQuery(defaults);
                    }}
                  >
                    清空筛选
                  </button>
                )
              }
            />
          )
        )}
        <footer className="pagination">
          <span>
            {loading
              ? "正在更新…"
              : page
                ? `共 ${page.total} 位用户`
                : "用户列表"}
            <span className="pagination-time"> · 时间为北京时间</span>
          </span>
          <div>
            <label>
              每页
              <select
                aria-label="每页数量"
                value={query.page_size}
                onChange={(event) =>
                  setQuery((previous) => ({
                    ...previous,
                    page: 1,
                    page_size: Number(event.target.value),
                  }))
                }
              >
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
              条
            </label>
            <span className="page-indicator">
              {query.page} / {pageCount}
            </span>
            <button
              className="icon-button bordered"
              aria-label="上一页"
              disabled={loading || query.page <= 1}
              onClick={() =>
                setQuery((value) => ({ ...value, page: value.page - 1 }))
              }
            >
              <ChevronLeft size={16} />
            </button>
            <button
              className="icon-button bordered"
              aria-label="下一页"
              disabled={loading || !page || query.page >= pageCount}
              onClick={() =>
                setQuery((value) => ({ ...value, page: value.page + 1 }))
              }
            >
              <ChevronRight size={16} />
            </button>
          </div>
        </footer>
      </section>
      <div className="page-footnote">
        <ShieldCheck size={14} />
        角色和账号状态的变更，将应用于用户的后续操作。
      </div>
      {dialog && (
        <UserDialog
          state={dialog}
          api={api}
          onClose={() => setDialog(null)}
          onSaved={saved}
        />
      )}
    </>
  );
}
