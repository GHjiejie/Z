import { useCallback, useEffect, useState } from "react";
import { Plus, RefreshCw, ShieldCheck } from "lucide-react";
import { aborted, ApiClient, type RoleDefinition } from "./api";
import { EmptyState, ErrorBanner, PermissionList, Spinner } from "./components";
import { RoleDialog, type RoleDialogState } from "./RoleDialog";

export function RolesPage({
  api,
  permissions,
  notify,
  onPermissionsChanged,
}: {
  api: ApiClient;
  permissions: string[];
  notify: (message: string) => void;
  onPermissionsChanged: () => void;
}) {
  const [roles, setRoles] = useState<RoleDefinition[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [dialog, setDialog] = useState<RoleDialogState | null>(null);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void api
      .roles(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) {
          setRoles(data);
          setLoading(false);
        }
      })
      .catch((failure) => {
        if (!aborted(failure) && !controller.signal.aborted) {
          setError(failure);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [api, revision]);
  useEffect(() => {
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, [refresh]);
  function saved(message: string) {
    setDialog(null);
    notify(message);
    refresh();
    onPermissionsChanged();
  }
  return (
    <>
      <header className="page-heading">
        <div>
          <div className="eyebrow">访问控制 · RBAC</div>
          <h1>角色与权限</h1>
          <p>为角色选择操作权限，再将一个或多个角色分配给用户</p>
        </div>
        <div className="role-page-actions">
          <button
            className="button secondary"
            disabled={loading}
            onClick={refresh}
          >
            <RefreshCw size={16} className={loading ? "spin" : ""} />
            刷新角色
          </button>
          {permissions.includes("role.create") && (
            <button
              className="button primary"
              onClick={() => setDialog({ kind: "create" })}
            >
              <Plus size={16} />
              新增角色
            </button>
          )}
        </div>
      </header>
      {Boolean(error) && <ErrorBanner error={error} onRetry={refresh} />}
      {loading && !roles && (
        <section className="panel center-loading">
          <Spinner label="正在加载角色权限" />
        </section>
      )}
      {roles?.length === 0 && (
        <EmptyState
          title="还没有角色"
          description="创建角色后，为用户分配允许执行的操作"
        />
      )}
      <div className="roles-grid" aria-busy={loading}>
        {roles?.map((role) => {
          const withinScope = role.permissions.every((value) =>
            permissions.includes(value),
          );
          return (
            <section
              className="panel role-card"
              key={role.code}
              aria-label={`角色：${role.display_name}`}
            >
              <div className="role-card-toolbar">
                <span className="role-card-icon">
                  <ShieldCheck size={18} />
                </span>
                <span className="built-in-label">
                  {role.built_in ? "内置角色" : "自定义角色"}
                </span>
              </div>
              <div className="role-card-title">
                <h2>{role.display_name}</h2>
                <span className="count-badge">
                  {role.permissions.length} 项权限
                </span>
              </div>
              <p className="role-code">
                {role.code} · {role.user_count} 位用户
              </p>
              <p>{role.description || "暂无角色说明"}</p>
              <div className="role-card-divider" />
              <h3>允许的操作</h3>
              {role.permissions.length ? (
                <PermissionList permissions={role.permissions} />
              ) : (
                <p>尚未授予操作权限</p>
              )}
              <div className="role-card-actions">
                {role.code === "admin" ? (
                  <span className="field-hint">系统管理员角色受保护</span>
                ) : (
                  <>
                    {permissions.includes("role.update") && (
                      <button
                        className="button secondary"
                        disabled={!withinScope}
                        title={
                          !withinScope
                            ? "此角色包含超出你自身范围的权限"
                            : undefined
                        }
                        onClick={() =>
                          setDialog({ kind: "edit", code: role.code })
                        }
                      >
                        编辑角色
                      </button>
                    )}
                    {permissions.includes("role.delete") && !role.built_in && (
                      <button
                        className="text-button danger-text"
                        disabled={!withinScope || role.user_count > 0}
                        title={
                          role.user_count
                            ? "请先调整所有使用此角色的用户"
                            : undefined
                        }
                        onClick={() =>
                          setDialog({ kind: "delete", code: role.code })
                        }
                      >
                        删除角色
                      </button>
                    )}
                  </>
                )}
              </div>
            </section>
          );
        })}
      </div>
      <div className="role-explanation">
        <ShieldCheck size={18} />
        <div>
          <h3>用户 → 角色 → 权限</h3>
          <p>
            用户的有效权限为所有角色权限的并集。角色权限和用户角色的变更在后续请求立即生效；禁用账号将撤销登录会话。
          </p>
        </div>
      </div>
      {dialog && (
        <RoleDialog
          state={dialog}
          api={api}
          granted={permissions}
          onClose={() => setDialog(null)}
          onSaved={saved}
        />
      )}
    </>
  );
}
