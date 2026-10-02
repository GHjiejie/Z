import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import type { LucideIcon } from "lucide-react";
import { getScopeRevision, scopeIsCurrent, tenantLink, write } from "./api";
import {
  Button,
  Empty,
  ErrorState,
  Field,
  Modal,
  ResourceState,
  useResource,
} from "./components";
import type { Agent, Model } from "./types";
import "./agent-management.css";

type Items<T> = { items: T[] };
export type AgentView = {
  mode: "list" | "detail" | "create" | "edit";
  title: string;
};
const query = () => new URLSearchParams(location.hash.split("?")[1] ?? "");
export function DesignIcon({
  file,
  className = "",
  directory = "agent-design",
}: {
  file: string;
  className?: string;
  directory?: string;
}) {
  return (
    <img
      src={`/${directory}/${file}.svg`}
      className={`agent-design-icon ${className}`}
      alt=""
    />
  );
}

// These are the original exported variants, not substitute icon drawings.
const menuAssets: Record<string, [string, string]> = {
  overview: ["5fe48", "883ee"],
  agents: ["69b6e", "cee82"],
  playground: ["fd720", "37dc8"],
  runs: ["abc52", "984d6"],
  models: ["717d1", "373b9"],
  usage: ["d216d", "d7c49"],
  billing: ["d33ba", "82a74"],
  users: ["2fb60", "b9788"],
  quotas: ["283c5", "4bf5a"],
  audit: ["85034", "4fa42"],
  "support-access": ["d6808", "64b08"],
};
const menuLabels: Record<string, string> = {
  overview: "概览 Dashboard",
  agents: "Agent管理",
  playground: "对话实验室",
  runs: "我的运行记录",
  models: "模型策略",
  usage: "调用统计与导出",
  billing: "费用中心",
  users: "成员与邀请",
  quotas: "组织配额",
  audit: "审计日志",
  "support-access": "Owner支持审批",
};
export function AgentSidebar({
  items,
  collapsed,
  toggle,
  list,
  activeRoute = "agents",
  icons,
  iconsDirectory = "agent-design",
}: {
  items: { route: string; title: string; group: string; icon: LucideIcon }[];
  collapsed: boolean;
  toggle: () => void;
  list: boolean;
  activeRoute?: string;
  icons?: Record<string, string>;
  iconsDirectory?: string;
}) {
  return (
    <>
      <div className="agent-brand-row">
        {collapsed ? (
          <button
            className="agent-logo-button"
            aria-label="展开侧栏"
            onClick={toggle}
          >
            <span className="agent-logo">Z</span>
          </button>
        ) : (
          <>
            <a className="agent-brand" href={tenantLink("overview")}>
              <span className="agent-logo">Z</span>
              <span>Z Agent</span>
            </a>
            <button
              className="agent-collapse"
              aria-label="收起侧栏"
              onClick={toggle}
            >
              <DesignIcon file="f4b93" />
            </button>
          </>
        )}
      </div>
      <nav className="agent-nav" aria-label="主导航">
        {["工作空间", "资源与费用", "组织管理"].map((group) => {
          const groupItems = items.filter((item) => item.group === group);
          return groupItems.length ? (
            <div className="agent-nav-group" key={group}>
              {!collapsed && <div className="agent-nav-label">{group}</div>}
              {groupItems.map((item) => (
                <div key={item.route}>
                  <a
                    href={tenantLink(item.route)}
                    className={`agent-nav-link ${item.route === activeRoute ? "selected" : ""}`}
                    aria-label={
                      collapsed
                        ? (menuLabels[item.route] ?? item.title)
                        : undefined
                    }
                    aria-current={item.route === activeRoute ? "page" : undefined}
                  >
                    {icons?.[item.route] ? (
                      <DesignIcon file={icons[item.route]} directory={iconsDirectory} />
                    ) : menuAssets[item.route] ? (
                      <DesignIcon file={menuAssets[item.route][list ? 0 : 1]} />
                    ) : (
                      <item.icon size={16} />
                    )}
                    {!collapsed && (
                      <span>{menuLabels[item.route] ?? item.title}</span>
                    )}
                  </a>
                  {!collapsed && item.route === "playground" && (
                    <div className="agent-subnav">
                      <a href={tenantLink("playground")}>{icons ? "+ 新建会话" : "＋ 新建会话"}</a>
                      <a href={tenantLink("playground")}>会话记录</a>
                      <a href={tenantLink("playground")}>
                        <span>运行轨迹</span>
                        <small>当前会话</small>
                      </a>
                    </div>
                  )}
                </div>
              ))}
            </div>
          ) : null;
        })}
      </nav>
    </>
  );
}

export function AgentManagement({
  admin,
  canRun,
  navigate,
  onViewChange,
}: {
  admin: boolean;
  canRun: boolean;
  navigate: (path: string) => void;
  onViewChange: (view: AgentView) => void;
}) {
  const agents = useResource<Items<Agent>>("/agents");
  const models = useResource<Items<Model>>("/models");
  const [params, setParams] = useState(query);
  const [search, setSearch] = useState("");
  const [source, setSource] = useState("all");
  const [confirmation, setConfirmation] = useState<Agent | null>(null);
  const [publishing, setPublishing] = useState(false);
  const [publishError, setPublishError] = useState("");
  const [notice, setNotice] = useState("");
  const [published, setPublished] = useState<{
    name: string;
    version: number;
  } | null>(null);
  const publishLock = useRef(false);
  const current = agents.data?.items.find(
    (agent) => agent.id === params.get("agent"),
  );
  const mode: AgentView["mode"] =
    params.get("agent") === "new"
      ? "create"
      : params.has("agent")
        ? params.get("edit") === "1"
          ? "edit"
          : "detail"
        : "list";
  useEffect(() => {
    const update = () => {
      setParams(query());
      setNotice("");
      setPublished(null);
      setConfirmation(null);
    };
    window.addEventListener("hashchange", update);
    return () => window.removeEventListener("hashchange", update);
  }, []);
  useEffect(() => {
    onViewChange({
      mode,
      title:
        mode === "create"
          ? "创建Agent"
          : mode === "edit"
            ? "编辑配置"
            : (current?.name ?? ""),
    });
  }, [mode, current?.name, onViewChange]);
  const open = (agent?: Agent, edit = false) =>
    navigate(
      agent
        ? `agents?agent=${encodeURIComponent(agent.id)}${edit ? "&edit=1" : ""}`
        : "agents?agent=new",
    );
  const modelName = (agent: Agent) =>
    agent.model_id
      ? (models.data?.items.find((model) => model.id === agent.model_id)
          ?.name ?? agent.model_id)
      : "跟随组织策略";
  async function publish() {
    if (!confirmation || publishing || publishLock.current || !admin) return;
    const revision = getScopeRevision();
    publishLock.current = true;
    setPublishing(true);
    setPublishError("");
    try {
      const result = await write<{ version: number }>(
        `/agents/${encodeURIComponent(confirmation.id)}/publish`,
      );
      if (!scopeIsCurrent(revision)) return;
      setPublished({ name: confirmation.name, version: result.version });
      setConfirmation(null);
      await agents.reload();
    } catch (error) {
      if (scopeIsCurrent(revision)) setPublishError((error as Error).message);
    } finally {
      publishLock.current = false;
      setPublishing(false);
    }
  }
  const items =
    agents.data?.items.filter(
      (agent) =>
        (source === "all" ||
          (source === "builtin"
            ? Boolean(agent.builtin_key)
            : !agent.builtin_key)) &&
        `${agent.name} ${agent.id} ${agent.description} ${agent.category ?? ""}`
          .toLowerCase()
          .includes(search.toLowerCase()),
    ) ?? [];
  const saved = useCallback(
    async (agent: Agent) => {
      // Use the persisted response immediately; a failed list refresh must not imply a failed save.
      agents.setData((value) => ({
        items: [
          ...(value?.items ?? []).filter((item) => item.id !== agent.id),
          agent,
        ],
      }));
      navigate(`agents?agent=${encodeURIComponent(agent.id)}`);
      await agents.reload();
      setNotice("配置已保存为草稿。发布后，后续运行使用新版本。");
    },
    [agents.setData, agents.reload, navigate],
  );
  return (
    <div
      className={`agent-management agent-${mode}`}
      data-testid={`agent-${mode}`}
    >
      {notice && (
        <div className="agent-feedback" role="status">
          {notice}
          <button aria-label="关闭保存通知" onClick={() => setNotice("")}>
            ×
          </button>
        </div>
      )}
      {mode === "create" || mode === "edit" ? (
        !admin ? (
          <Empty
            title="无管理权限"
            description="仅组织 owner 和管理员可创建、修改和发布 Agent。"
            action={
              <Button onClick={() => navigate("agents")}>返回列表</Button>
            }
          />
        ) : mode === "edit" && !current ? (
          <ResourceState
            loading={agents.loading}
            error={agents.error}
            retry={() => void agents.reload()}
          >
            <Empty title="Agent 不存在或无权查看" />
          </ResourceState>
        ) : (
          <AgentForm
            key={current?.id ?? "new"}
            agent={mode === "edit" ? (current ?? null) : null}
            models={models.data?.items ?? []}
            modelsError={models.error}
            modelsLoading={models.loading}
            retryModels={() => void models.reload()}
            cancel={() => (current ? open(current) : navigate("agents"))}
            saved={saved}
          />
        )
      ) : mode === "detail" ? (
        <ResourceState
          loading={agents.loading}
          error={agents.error}
          retry={() => void agents.reload()}
        >
          <Button
            variant="secondary"
            className="agent-back"
            onClick={() => navigate("agents")}
          >
            <DesignIcon file="0b7f0" />
            返回 Agent 列表
          </Button>
          {current ? (
            <>
              <div className="agent-detail-heading">
                <div>
                  <h1>{current.name}</h1>
                  <code>{current.id}</code>
                  <a
                    className={`agent-version ${current.published_version ? "published" : ""}`}
                    href={
                      canRun && current.published_version
                        ? tenantLink(
                            `playground?agent=${encodeURIComponent(current.id)}`,
                          )
                        : undefined
                    }
                    aria-label={
                      canRun && current.published_version
                        ? `已发布 v${current.published_version}，在对话实验室运行`
                        : undefined
                    }
                  >
                    {current.published_version
                      ? `● 已发布 v${current.published_version}`
                      : "未发布"}
                  </a>
                </div>
                {admin && (
                  <div className="agent-actions">
                    <Button
                      variant="secondary"
                      onClick={() => open(current, true)}
                    >
                      <DesignIcon file="0c6ea" />
                      编辑配置
                    </Button>
                    <Button
                      onClick={() => {
                        setConfirmation(current);
                        setPublishError("");
                      }}
                    >
                      <DesignIcon file="2ca5b" />
                      {current.published_version ? "发布新版本" : "发布 Agent"}
                    </Button>
                  </div>
                )}
              </div>
              <div className="agent-detail-grid">
                <section
                  className="agent-panel agent-basic"
                  data-node-id="14:673"
                >
                  <h2>基本配置</h2>
                  <dl>
                    <dt>Agent 名称</dt>
                    <dd className="agent-name-value">{current.name}</dd>
                    <dt>说明描述</dt>
                    <dd>{current.description || "尚未填写说明"}</dd>
                    <dt>系统提示词 (Prompt)</dt>
                    <dd className="agent-prompt">{current.system_prompt}</dd>
                  </dl>
                </section>
                <section
                  className="agent-panel agent-execution"
                  data-node-id="14:735"
                >
                  <h2>执行配置</h2>
                  <dl>
                    {[
                      ["模型偏好", modelName(current)],
                      ["Temperature", current.temperature],
                      ["最大步骤 (Max Steps)", current.max_steps],
                      ["最大输出 Token", current.max_tokens],
                    ].map(([label, value]) => (
                      <div className="agent-setting" key={label}>
                        <dt>{label}</dt>
                        <dd
                          className={
                            label === "模型偏好"
                              ? "agent-model-value"
                              : "agent-mono"
                          }
                        >
                          {value}
                        </dd>
                      </div>
                    ))}
                    <dt className="agent-tools-label">允许使用的工具</dt>
                    <dd className="agent-tools">
                      {current.tools.length ? (
                        current.tools.map((tool) => (
                          <span key={tool}>
                            {tool === "calculator" ||
                            tool === "current_time" ? (
                              <DesignIcon
                                file={tool === "calculator" ? "acca8" : "bff7d"}
                              />
                            ) : null}
                            <code>{tool}</code>
                          </span>
                        ))
                      ) : (
                        <span>未启用工具</span>
                      )}
                    </dd>
                  </dl>
                </section>
              </div>
              <p className="agent-footnote">
                <DesignIcon file="9f186" />
                保存配置不会自动发布；发布成功后返回新的整数版本。
              </p>
            </>
          ) : (
            <Empty
              title="Agent 不存在或无权查看"
              description="成员只能查看已发布版本，请返回列表选择可访问的 Agent。"
            />
          )}
        </ResourceState>
      ) : (
        <>
          <header className="agent-list-heading">
            <div>
              <h1>Agent管理</h1>
              <p>配置 Agent 提示词、工具与执行参数</p>
            </div>
            {admin && <Button onClick={() => open()}>+ 创建 Agent</Button>}
          </header>
          <div className="agent-list-toolbar">
            <label className="agent-search">
              <DesignIcon file="1f5ed" />
              <input
                aria-label="在已加载的 Agent 中搜索"
                placeholder="在已加载的 Agent 中搜索"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <details className="agent-list-options">
              <summary>已加载 {agents.data?.items.length ?? 0} 个</summary>
              <div className="agent-list-controls">
                <select
                  aria-label="筛选 Agent 来源"
                  value={source}
                  onChange={(event) => setSource(event.target.value)}
                >
                  <option value="all">全部 Agent</option>
                  <option value="builtin">内置 Agent</option>
                  <option value="custom">自建 Agent</option>
                </select>
                <button
                  onClick={() => void agents.reload()}
                  aria-label="刷新 Agent"
                >
                  刷新
                </button>
              </div>
            </details>
          </div>
          <ResourceState
            loading={agents.loading}
            error={agents.error}
            retry={() => void agents.reload()}
          >
            {items.length ? (
              <div className="agent-table-wrap">
                <table className="agent-table">
                  <colgroup>
                    <col style={{ width: "24%" }} />
                    <col style={{ width: "32%" }} />
                    <col style={{ width: "18%" }} />
                    <col style={{ width: "14%" }} />
                    <col style={{ width: "12%" }} />
                  </colgroup>
                  <thead>
                    <tr>
                      <th>名称</th>
                      <th>说明</th>
                      <th>模型偏好</th>
                      <th>发布版本</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((agent) => (
                      <tr key={agent.id}>
                        <td>
                          <strong>{agent.name}</strong>
                          <code>{agent.id}</code>
                        </td>
                        <td
                          className={!agent.description ? "agent-subtle" : ""}
                        >
                          {agent.description || "尚未填写说明"}
                        </td>
                        <td className={!agent.model_id ? "agent-subtle" : ""}>
                          {modelName(agent)}
                        </td>
                        <td>
                          <span
                            className={`agent-version ${agent.published_version ? "published" : ""}`}
                          >
                            {agent.published_version
                              ? `v${agent.published_version}`
                              : "未发布"}
                          </span>
                        </td>
                        <td>
                          <a
                            href={tenantLink(
                              `agents?agent=${encodeURIComponent(agent.id)}`,
                            )}
                          >
                            查看详情
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title={
                  search || source !== "all"
                    ? "未找到匹配的 Agent"
                    : "还没有 Agent"
                }
                description="可调整搜索条件，或由管理员创建并发布 Agent。"
              />
            )}
          </ResourceState>
          <p className="agent-footnote">
            <DesignIcon file="735b6" />
            仅已发布 Agent 可用于对话实验室
          </p>
        </>
      )}
      {confirmation && (
        <Modal
          title="确认发布 Agent"
          close={() => {
            if (!publishing) setConfirmation(null);
          }}
          description="发布会保存一个不可变版本，后续新运行使用该版本；已有运行不受影响。"
        >
          <div className="agent-dialog-body">
            <p>
              将「{confirmation.name}」发布为 v
              {(confirmation.published_version ?? 0) + 1}？
            </p>
            {publishError && (
              <div className="agent-form-error" role="alert">
                {publishError}
              </div>
            )}
          </div>
          <div className="agent-dialog-footer">
            <Button
              variant="secondary"
              disabled={publishing}
              onClick={() => setConfirmation(null)}
            >
              取消
            </Button>
            <Button busy={publishing} onClick={() => void publish()}>
              确认发布
            </Button>
          </div>
        </Modal>
      )}
      {published && (
        <Modal title="发布成功" close={() => setPublished(null)}>
          <div className="agent-dialog-body">
            <p>
              「{published.name}」已发布为 <strong>v{published.version}</strong>
              。
            </p>
            <p>后续新运行将使用该版本。</p>
          </div>
          <div className="agent-dialog-footer">
            <Button onClick={() => setPublished(null)}>完成</Button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function AgentForm({
  agent,
  models,
  modelsError,
  modelsLoading,
  retryModels,
  cancel,
  saved,
}: {
  agent: Agent | null;
  models: Model[];
  modelsError: string;
  modelsLoading: boolean;
  retryModels: () => void;
  cancel: () => void;
  saved: (agent: Agent) => Promise<void>;
}) {
  const [selectedModel, setSelectedModel] = useState(agent?.model_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [invalid, setInvalid] = useState<Record<string, string>>({});
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const modelLimit =
    models.find((model) => model.id === selectedModel)?.max_output_tokens ??
    128000;
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy || lock.current) return;
    const form = event.currentTarget;
    const errors: Record<string, string> = {};
    for (const control of Array.from(form.elements)) {
      if (
        control instanceof HTMLInputElement ||
        control instanceof HTMLTextAreaElement ||
        control instanceof HTMLSelectElement
      ) {
        if (!control.checkValidity())
          errors[control.name] = control.validationMessage;
        else if (
          ["name", "system_prompt"].includes(control.name) &&
          !control.value.trim()
        )
          errors[control.name] = "请填写内容，不能仅含空白。";
      }
    }
    setInvalid(errors);
    setError("");
    if (Object.keys(errors).length) {
      form
        .querySelector<HTMLElement>(`[name="${Object.keys(errors)[0]}"]`)
        ?.focus();
      return;
    }
    const revision = getScopeRevision();
    const data = new FormData(form);
    const text = (key: string) => String(data.get(key) ?? "");
    lock.current = true;
    setBusy(true);
    try {
      const result = await write<Agent>(
        agent ? `/agents/${encodeURIComponent(agent.id)}` : "/agents",
        {
          name: text("name"),
          description: text("description"),
          system_prompt: text("system_prompt"),
          model_id: text("model_id") || null,
          temperature: Number(text("temperature")),
          max_steps: Number(text("max_steps")),
          max_tokens: Number(text("max_tokens")),
          tools: data.getAll("tools"),
        },
        agent ? "PATCH" : "POST",
      );
      if (mounted.current && scopeIsCurrent(revision)) await saved(result);
    } catch (err) {
      if (mounted.current && scopeIsCurrent(revision))
        setError((err as Error).message);
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  const validation = (name: string) => ({
    "aria-invalid": Boolean(invalid[name]),
    "aria-errormessage": invalid[name] ? `error-${name}` : undefined,
  });
  const fieldError = (name: string) =>
    invalid[name] && (
      <small className="agent-field-error" id={`error-${name}`}>
        {invalid[name]}
      </small>
    );
  return (
    <form className="agent-form" noValidate onSubmit={submit}>
      <header className="agent-form-heading">
        <h1>{agent ? "编辑配置" : "创建Agent"}</h1>
        <p>
          配置 Agent 基础信息与执行参数。
          {agent
            ? "修改后先保存草稿，再单独发布。"
            : "创建后先保存配置，再单独发布。"}
        </p>
      </header>
      <fieldset disabled={busy} className="agent-form-grid">
        <section className="agent-panel">
          <h2>基础信息</h2>
          <div className="agent-form-field">
            <Field
              label="Agent 名称"
              hint="1–120 字符，用于在列表与选择器中标识 Agent"
            >
              <input
                name="name"
                required
                maxLength={120}
                defaultValue={agent?.name ?? ""}
                {...validation("name")}
              />
            </Field>
            {fieldError("name")}
          </div>
          <div className="agent-form-field">
            <Field label="说明描述" hint="可选，不超过 2000 字符">
              <input
                name="description"
                maxLength={2000}
                defaultValue={agent?.description ?? ""}
                {...validation("description")}
              />
            </Field>
            {fieldError("description")}
          </div>
          <div className="agent-form-field">
            <Field
              label="系统提示词 (Prompt)"
              hint="1–20000 字符，定义 Agent 角色行为与回答风格"
            >
              <textarea
                name="system_prompt"
                required
                maxLength={20000}
                defaultValue={
                  agent?.system_prompt ??
                  "你是一位严谨、友好的智能助手。请用中文清晰地回答用户的问题。"
                }
                {...validation("system_prompt")}
              />
            </Field>
            {fieldError("system_prompt")}
          </div>
        </section>
        <section className="agent-panel">
          <h2>执行参数</h2>
          <div className="agent-form-field">
            <Field label="模型偏好" hint="缺省时按跟随所属组织的统一模型偏好">
              <select
                name="model_id"
                value={selectedModel}
                onChange={(event) => setSelectedModel(event.target.value)}
              >
                <option value="">跟随组织策略（可选）</option>
                {agent?.model_id &&
                  !models.some((model) => model.id === agent.model_id) && (
                    <option value={agent.model_id}>
                      {agent.model_id}（当前偏好，模型不可用）
                    </option>
                  )}
                {models
                  .filter(
                    (model) => model.active || model.id === agent?.model_id,
                  )
                  .map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.name}
                      {!model.active ? "（已停用）" : ""}
                    </option>
                  ))}
              </select>
            </Field>
          </div>
          {modelsLoading && (
            <p className="agent-subtle" role="status">
              正在加载可用模型…
            </p>
          )}
          {modelsError && (
            <ErrorState message={modelsError} retry={retryModels} />
          )}
          <div className="agent-number-fields">
            {(
              [
                [
                  "temperature",
                  "Temperature",
                  0,
                  2,
                  0.1,
                  agent?.temperature ?? 1,
                  "范围 0–2",
                ],
                [
                  "max_steps",
                  "最大步骤",
                  1,
                  30,
                  1,
                  agent?.max_steps ?? 6,
                  "范围 1–30",
                ],
                [
                  "max_tokens",
                  "最大输出 Token",
                  1,
                  modelLimit,
                  1,
                  agent?.max_tokens ?? 2048,
                  "不超过模型上限",
                ],
              ] as const
            ).map(([name, label, min, max, step, value, hint]) => (
              <div key={name}>
                <Field label={label} hint={hint}>
                  <input
                    name={name}
                    type="number"
                    min={min}
                    max={max}
                    step={step}
                    required
                    defaultValue={value}
                    {...validation(name)}
                  />
                </Field>
                {fieldError(name)}
              </div>
            ))}
          </div>
          <div className="agent-tool-field">
            <span>允许使用的工具</span>
            {["calculator", "current_time"].map((tool) => (
              <label key={tool}>
                <input
                  type="checkbox"
                  name="tools"
                  value={tool}
                  defaultChecked={agent?.tools.includes(tool) ?? false}
                />
                <code>{tool}</code>
              </label>
            ))}
          </div>
        </section>
      </fieldset>
      {Object.keys(invalid).length > 0 && (
        <div className="agent-form-error" role="alert">
          请检查标记的字段后再保存。
        </div>
      )}
      {error && (
        <div className="agent-form-error" role="alert">
          {error}
        </div>
      )}
      <div className="agent-savebar">
        <Button type="submit" busy={busy}>
          保存配置
        </Button>
        <Button
          type="button"
          variant="secondary"
          disabled={busy}
          onClick={cancel}
        >
          取消
        </Button>
      </div>
    </form>
  );
}
