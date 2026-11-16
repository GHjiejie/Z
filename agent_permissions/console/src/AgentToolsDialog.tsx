import { useCallback, useEffect, useRef, useState } from "react";
import { Info, RefreshCw, Search } from "lucide-react";
import {
  aborted,
  ApiClient,
  ApiError,
  type AgentToolGrant,
  type User,
  type UserAgentToolGrants,
  type RoleOption,
} from "./api";
import {
  Avatar,
  EmptyState,
  ErrorBanner,
  Modal,
  roleLabel,
  Spinner,
} from "./components";

function statusLabel(item: AgentToolGrant, enabled: boolean): string {
  if (!item.available) return "工具已下线";
  if (item.state === "account_disabled") return "账号已禁用";
  if (!enabled) return "未授权";
  if (!item.rbac_allowed) return "已允许，当前缺少业务权限";
  return "已允许，业务权限满足";
}

export function AgentToolsDialog({
  user,
  api,
  permissions,
  onClose,
  onSaved,
}: {
  user: User;
  api: ApiClient;
  permissions: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [target, setTarget] = useState(user);
  const [roles, setRoles] = useState<RoleOption[]>([]);
  const [view, setView] = useState<UserAgentToolGrants | null>(null);
  const [draft, setDraft] = useState<Record<string, boolean>>({});
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const locked = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const saveController = useRef<AbortController | null>(null);
  const load = useCallback(
    (reset = true) => {
      if (locked.current) return;
      controller.current?.abort();
      const next = new AbortController();
      controller.current = next;
      if (reset) setLoading(true);
      else setRefreshing(true);
      setLoadError(null);
      if (reset) setError(null);
      void Promise.all([
        api.userAgentTools(user.id, next.signal),
        api.getUser(user.id, next.signal),
        api.roleOptions(next.signal),
      ])
        .then(([data, record, options]) => {
          if (next.signal.aborted) return;
          setTarget(record.user);
          setRoles(options);
          if (reset) {
            setView(data);
            setDraft(
              Object.fromEntries(
                data.items.map((item) => [item.operation_id, item.enabled]),
              ),
            );
          } else {
            setView((previous) =>
              previous
                ? {
                    ...data,
                    version: previous.version,
                    catalog_revision: previous.catalog_revision,
                    items: data.items.map((item) => ({
                      ...item,
                      enabled:
                        previous.items.find(
                          (old) => old.operation_id === item.operation_id,
                        )?.enabled ?? item.enabled,
                    })),
                  }
                : data,
            );
          }
        })
        .catch((failure: unknown) => {
          if (!aborted(failure) && !next.signal.aborted) {
            if (reset) setLoadError(failure);
            else setError(failure);
          }
        })
        .finally(() => {
          if (!next.signal.aborted) {
            setLoading(false);
            setRefreshing(false);
          }
        });
    },
    [api, user.id],
  );
  useEffect(() => {
    load();
    return () => {
      controller.current?.abort();
      saveController.current?.abort();
    };
  }, [load]);

  const canManage = Boolean(
    view?.can_manage && permissions.includes("user.agent_tools.manage"),
  );
  const changes =
    view?.items.flatMap((item) => {
      const enabled = draft[item.operation_id] ?? item.enabled;
      return enabled === item.enabled
        ? []
        : [{ operation_id: item.operation_id, enabled }];
    }) ?? [];
  const conflict =
    error instanceof ApiError &&
    ["VERSION_CONFLICT", "TOOL_VERSION_MISMATCH"].includes(error.code);
  const visible =
    view?.items.filter((item) =>
      `${item.display_name} ${item.operation_id}`
        .toLocaleLowerCase()
        .includes(search.trim().toLocaleLowerCase()),
    ) ?? [];
  const groups = [...new Set(visible.map((item) => item.resource))];
  const enabledCount =
    view?.items.filter((item) => draft[item.operation_id] ?? item.enabled)
      .length ?? 0;
  const readyCount =
    view?.items.filter(
      (item) =>
        (draft[item.operation_id] ?? item.enabled) &&
        item.rbac_allowed &&
        item.available &&
        target.status === "active",
    ).length ?? 0;

  async function save() {
    if (
      locked.current ||
      loading ||
      loadError !== null ||
      !view ||
      !canManage ||
      !changes.length ||
      conflict ||
      refreshing
    )
      return;
    controller.current?.abort();
    locked.current = true;
    setBusy(true);
    setError(null);
    const next = new AbortController();
    saveController.current = next;
    try {
      await api.updateUserAgentTools(user.id, view, changes, next.signal);
      if (!next.signal.aborted) onSaved();
    } catch (failure) {
      if (!aborted(failure) && !next.signal.aborted) setError(failure);
    } finally {
      locked.current = false;
      if (!next.signal.aborted) setBusy(false);
    }
  }

  return (
    <Modal
      title="Agent 工具授权"
      busy={busy}
      onClose={onClose}
      className="agent-tools-modal"
      description="按用户配置 Agent 可以代为调用的工具"
      footer={
        <>
          <button
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            {canManage ? "取消" : "关闭"}
          </button>
          {canManage && (
            <button
              className="button primary"
              disabled={
                busy ||
                loading ||
                loadError !== null ||
                refreshing ||
                !changes.length ||
                conflict
              }
              onClick={() => void save()}
            >
              {busy ? "正在保存…" : "保存授权"}
            </button>
          )}
        </>
      }
    >
      {loading ? (
        <Spinner label="正在读取工具授权" />
      ) : loadError ? (
        <ErrorBanner error={loadError} onRetry={() => load()} />
      ) : (
        view && (
          <div className="agent-tools-config">
            <div className="target-user">
              <Avatar user={target} />
              <div>
                <strong>{target.display_name}</strong>
                <small>
                  {target.username} ·{" "}
                  {target.roles
                    .map(
                      (code) =>
                        roles.find((role) => role.code === code)
                          ?.display_name ?? roleLabel(code),
                    )
                    .join("、")}
                </small>
              </div>
            </div>
            <p className="info-note">
              <Info size={16} />
              <span>
                允许 Agent
                代此用户调用工具。实际操作仍受该用户当前角色权限限制；角色变化不会清除这里的设置。
              </span>
            </p>
            {!canManage && (
              <p className="info-note">
                当前为只读模式，需要管理用户工具授权的权限，并满足目标用户的管理范围。
              </p>
            )}
            {target.status !== "active" && (
              <p className="info-note">账号已禁用，保存的设置暂不生效。</p>
            )}
            <div className="agent-tools-toolbar">
              <label className="search-control">
                <Search size={16} />
                <input
                  aria-label="搜索工具"
                  placeholder="搜索工具名称"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </label>
              <button
                className="button secondary compact"
                disabled={busy || refreshing}
                onClick={() => load(false)}
              >
                <RefreshCw size={14} />
                {refreshing ? "正在刷新…" : "刷新权限状态"}
              </button>
            </div>
            <p className="agent-tools-count" role="status">
              已允许 {enabledCount} 项 / 当前业务权限满足 {readyCount} 项
              {changes.length > 0 && ` · ${changes.length} 项未保存`}
            </p>
            {error !== null && <ErrorBanner error={error} />}
            {conflict && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => load()}
              >
                载入最新信息（替换草稿）
              </button>
            )}
            {!visible.length && (
              <EmptyState
                title="没有匹配的工具"
                description="尝试其他名称或清空搜索条件"
              />
            )}
            {groups.map((group) => {
              const items = visible.filter((item) => item.resource === group);
              const editable = items.filter((item) => item.available);
              return (
                <section
                  className="agent-tools-group"
                  key={group}
                  aria-label={group}
                >
                  <div className="agent-tools-group-heading">
                    <h3>{group}</h3>
                    {canManage && editable.length > 0 && (
                      <div>
                        <button
                          type="button"
                          className="text-button"
                          disabled={busy}
                          onClick={() =>
                            setDraft((current) => ({
                              ...current,
                              ...Object.fromEntries(
                                editable.map((item) => [
                                  item.operation_id,
                                  true,
                                ]),
                              ),
                            }))
                          }
                        >
                          允许本组{search.trim() ? "匹配项" : "工具"}
                        </button>
                        <button
                          type="button"
                          className="text-button"
                          disabled={busy}
                          onClick={() =>
                            setDraft((current) => ({
                              ...current,
                              ...Object.fromEntries(
                                items.map((item) => [item.operation_id, false]),
                              ),
                            }))
                          }
                        >
                          关闭本组{search.trim() ? "匹配项" : "工具"}
                        </button>
                      </div>
                    )}
                  </div>
                  {items.map((item) => {
                    const enabled = draft[item.operation_id] ?? item.enabled;
                    return (
                      <div
                        className={`agent-tool-row${enabled ? " selected" : ""}`}
                        key={item.operation_id}
                      >
                        <label className="agent-tool-choice">
                          <input
                            type="checkbox"
                            aria-label={`允许 Agent 调用：${item.display_name}`}
                            checked={enabled}
                            disabled={
                              busy ||
                              !canManage ||
                              (!item.available && !enabled)
                            }
                            onChange={(event) =>
                              setDraft((current) => ({
                                ...current,
                                [item.operation_id]: event.target.checked,
                              }))
                            }
                          />
                          <span>
                            <strong>{item.display_name}</strong>
                            <small>
                              {(
                                {
                                  read: "读取",
                                  write: "修改",
                                  destructive: "删除或敏感操作",
                                } as Record<string, string>
                              )[item.effect] ?? "已下线"}{" "}
                              · 当前业务权限：
                              {item.rbac_allowed ? "有权限" : "无权限"}
                            </small>
                            <details>
                              <summary>操作标识</summary>
                              <code>{item.operation_id}</code>
                            </details>
                          </span>
                        </label>
                        <span
                          className={`agent-tool-state${enabled && item.rbac_allowed && item.available && target.status === "active" ? " ready" : ""}`}
                        >
                          {statusLabel(item, enabled)}
                        </span>
                      </div>
                    );
                  })}
                </section>
              );
            })}
          </div>
        )
      )}
    </Modal>
  );
}
