import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { api, getScopeRevision, scopeIsCurrent, write } from "./api";
import { Button, Empty, ErrorState, Form, Modal, ResourceState, useResource } from "./components";
import { ModelPolicyEditor } from "./PlatformResources";
import { TenantExportsPanel } from "./OperationsPanel";
import { ResourceHeader, ResourceIcon, ResourceSearch, ResourceStatus, ResourceTabs, resourceMoney, resourceTime } from "./ResourcesUI";
import type { Ledger, Model, Usage, User, Wallet } from "./types";
import "./resources.css";

type Items<T> = { items: T[] };
type Policy = { default_model_id: string | null; ordered_model_ids: string[]; version: number };
type Call = Usage & { run_id: string; error?: string; finished_at?: string | null };
type Reservations = { id: string; call_id: string; run_id: string; status: string; amount: string; cost: string; reason: string; created_at: string };

export function ModelStrategyPage({ canPolicy }: { canPolicy: boolean }) {
  const models = useResource<Items<Model>>("/models");
  const policy = useResource<Policy>("/model-policy");
  const [editing, setEditing] = useState(false);
  const [changing, setChanging] = useState<Model | null>(null);
  const [search, setSearch] = useState("");
  const defaultModel = models.data?.items.find(model => model.id === policy.data?.default_model_id);
  const items = models.data?.items.filter(model => `${model.alias} ${model.name}`.toLowerCase().includes(search.toLowerCase())) ?? [];
  const reload = () => Promise.all([models.reload(), policy.reload()]);
  return <div className="resource-page resource-models" data-testid="resource-models">
    <ResourceHeader title="模型策略" description="管理已授权模型的组织偏好与选择优先级" />
    <section className="resource-policy">
      <div className="resource-policy-heading"><h2><i />组织偏好策略</h2>{canPolicy && <Button disabled={!policy.data || models.loading || !!models.error} onClick={() => setEditing(true)}><ResourceIcon file="3ebab" />编辑策略</Button>}</div>
      <ResourceState loading={policy.loading} error={policy.error} retry={() => void policy.reload()}>
        {policy.data && <>
          <div className="resource-policy-grid">
            <div className="resource-default"><div><span>默认模型</span><div><code>{defaultModel?.alias ?? (policy.data.default_model_id ? "已不可用模型" : "自动选择")}</code>{defaultModel && <ResourceStatus status={defaultModel.active ? "enabled" : "disabled"}>{defaultModel.active ? "已启用" : "已停用"}</ResourceStatus>}</div></div><p>未手动选择时按组织策略解析</p></div>
            <div className="resource-priority"><p>候选优先级顺序 <span>(ordered_models)</span></p><ol>{policy.data.ordered_model_ids.map((id, index) => {
              const model = models.data?.items.find(item => item.id === id);
              return <li key={id}><span className="resource-rank">{index + 1}</span><code>{model?.alias ?? id}</code>{id === policy.data?.default_model_id && <small>默认</small>}<span className="resource-model-name">{model?.name ?? "已不可用模型"}</span></li>;
            })}</ol>{policy.data.ordered_model_ids.length === 0 && <p className="resource-muted">按已授权模型的默认顺序选择</p>}</div>
          </div>
          <div className="resource-policy-note"><ResourceIcon file="6435e" /><span>选择优先级用于指定匹配候选顺序，不代表调用失败会自动重试或降级。</span><small>v{policy.data.version}</small></div>
        </>}
      </ResourceState>
    </section>
    <div className="resource-catalog-toolbar"><div><h2>已授权模型目录</h2><span>已授权 {models.data?.items.length ?? 0} 个模型</span></div><ResourceSearch value={search} onChange={setSearch} placeholder="筛选已加载模型..." icon="2ba58" /></div>
    <div className="resource-table-wrap"><ResourceState loading={models.loading} error={models.error} retry={() => void models.reload()}>
      {items.length ? <table className="resource-table resource-model-table"><colgroup>{[26,14,17,17,16,10].map((width,index) => <col key={index} style={{ width: `${width}%` }} />)}</colgroup><thead><tr><th>模型名称 / 别名</th><th>状态</th><th>输入单价</th><th>输出单价</th><th>上下文 / 最大输出</th><th>操作</th></tr></thead><tbody>{items.map(model => <tr key={model.id}><td><strong className="resource-mono">{model.alias}</strong><small>{model.name}</small></td><td><ResourceStatus status={model.active ? "enabled" : "disabled"}>{model.active ? "已启用" : "已停用"}</ResourceStatus></td><td><b className="resource-mono">{resourceMoney(model.input_price,2)}</b><span className="resource-muted"> / 百万 Tokens</span></td><td><b className="resource-mono">{resourceMoney(model.output_price,2)}</b><span className="resource-muted"> / 百万 Tokens</span></td><td><span className="resource-mono">{model.context_window.toLocaleString("en-US")}</span><span className="resource-muted"> / </span><span className="resource-mono">{model.max_output_tokens.toLocaleString("en-US")}</span><span className="resource-muted"> Tokens</span></td><td>{canPolicy ? <button className={`resource-model-toggle ${model.active ? "disable" : ""}`} onClick={() => setChanging(model)}>{model.active ? "停用" : "启用"}</button> : <span className="resource-muted">只读</span>}</td></tr>)}</tbody></table> : <Empty title={search ? "当前已加载模型无匹配" : "暂无已授权模型"} description="模型由平台运营人员授权；可调整筛选或联系管理员。" />}
    </ResourceState></div>
    <p className="resource-footnote">* 目录单价为当前组织的核算基准，停用模型将禁止新的会话调用，不会撤回授权或删除模型。</p>
    {editing && policy.data && <ModelPolicyEditor policy={policy.data} models={models.data?.items ?? []} close={() => setEditing(false)} saved={async () => { setEditing(false); await reload(); }} />}
    {changing && <Modal title={changing.active ? "停用模型" : "启用模型"} description={`将${changing.active ? "停用" : "启用"} ${changing.alias}。授权与历史记录会保留。`} close={() => setChanging(null)}><Form close={() => setChanging(null)} label="确认" submit={async () => { await write(`/models/${encodeURIComponent(changing.id)}`, { active: !changing.active }, "PATCH"); setChanging(null); await reload(); }}><p>此操作只修改当前组织的模型启用状态。</p></Form></Modal>}
  </div>;
}

export function ResourceBillingPage() {
  const wallet = useResource<Wallet>("/billing/wallet");
  const ledger = useResource<Items<Ledger>>("/billing/ledger");
  const reservations = useResource<Items<Reservations>>("/billing/reservations");
  const [tab, setTab] = useState("ledger");
  const [search, setSearch] = useState("");
  const entries = ledger.data?.items.filter(item => `${item.description} ${item.id}`.toLowerCase().includes(search.toLowerCase())) ?? [];
  return <div className="resource-page resource-billing" data-testid="resource-billing">
    <ResourceHeader title="费用中心" description="组织账户余额、预占额度与消费明细" action={<Button variant="secondary" onClick={() => void Promise.all([wallet.reload(), ledger.reload(), reservations.reload()])}><ResourceIcon file="9170d" />刷新</Button>} />
    <ResourceState loading={wallet.loading} error={wallet.error} retry={() => void wallet.reload()}>{wallet.data && <div className="resource-wallet-grid">
      {[{ label:"账户余额 (USD)", value:wallet.data.balance, icon:"000b2", note:wallet.data.blocked ? "账户暂停调用" : "账户可用", state:true },{ label:"当前预占 (Reserved)", value:wallet.data.reserved, icon:"68473", note:"运行中任务暂扣", state:false },{ label:"可用余额 (Available)", value:wallet.data.available, icon:"89769", note:"可用余额 = 余额 − 预占", state:false }].map(card => <section className="resource-wallet-card" key={card.label}><div><span>{card.label}</span><ResourceIcon file={card.icon} /></div><strong className="resource-mono">{resourceMoney(card.value)}</strong><p>{card.state && <i className={wallet.data?.blocked ? "blocked" : ""} />}{card.note}</p></section>)}
    </div>}</ResourceState>
    {wallet.data?.blocked && <div className="resource-warning" role="alert">钱包已暂停调用：{wallet.data.block_reason || "请联系平台财务处理。"}</div>}
    <ResourceTabs selected={tab} onChange={setTab} entries={[{value:"ledger",label:"资金流水"},{value:"reservations",label:<>预占记录 <small>{reservations.data?.items.length ?? 0}</small></>}]} />
    {tab === "ledger" ? <div className="resource-table-wrap">
      <div className="resource-ledger-toolbar"><span><i />最新变动记录（仅显示已入账流水）</span><ResourceSearch value={search} onChange={setSearch} placeholder="筛选流水说明..." icon="d3062" /></div>
      <ResourceState loading={ledger.loading} error={ledger.error} retry={() => void ledger.reload()}>{entries.length ? <table className="resource-table resource-ledger-table"><colgroup>{[20,14,18,18,30].map((width,index) => <col key={index} style={{width:`${width}%`}} />)}</colgroup><thead><tr><th>创建时间</th><th>类型</th><th>变动金额</th><th>变动后余额</th><th>说明</th></tr></thead><tbody>{entries.map(entry => <tr key={entry.id}><td className="resource-mono">{resourceTime(entry.created_at,true)}</td><td><ResourceStatus status={entry.type === "credit" ? "credit" : "charge"}>{entry.type === "credit" ? "入账" : entry.type === "charge" ? "扣费" : entry.type} <code>{entry.type}</code></ResourceStatus></td><td className={`resource-mono ${Number(entry.amount) > 0 ? "resource-positive" : ""}`}>{Number(entry.amount) > 0 ? "+ " : Number(entry.amount) < 0 ? "− " : ""}{resourceMoney(String(Math.abs(Number(entry.amount))))}</td><td className="resource-mono resource-muted">{resourceMoney(entry.balance)}</td><td>{entry.description || "—"}</td></tr>)}</tbody></table> : <Empty title={search ? "当前流水无匹配" : "还没有资金流水"} description="流水记录由真实入账和结算产生。" />}</ResourceState>
    </div> : <div className="resource-table-wrap"><ResourceState loading={reservations.loading} error={reservations.error} retry={() => void reservations.reload()}>{reservations.data?.items.length ? <table className="resource-table"><thead><tr><th>调用 / 运行</th><th>状态</th><th>预占金额</th><th>已结算费用</th><th>创建时间</th></tr></thead><tbody>{reservations.data.items.map(item => <tr key={item.id}><td><code>{item.call_id}</code><small>{item.run_id}</small>{item.reason && <small>{item.reason}</small>}</td><td><ResourceStatus status={item.status} /></td><td className="resource-mono">{resourceMoney(item.amount)}</td><td className="resource-mono">{["reserved","unresolved"].includes(item.status) ? "待核实" : resourceMoney(item.cost)}</td><td>{resourceTime(item.created_at,true)}</td></tr>)}</tbody></table> : <Empty title="暂无预占记录" description="模型调用准入时会产生预占。" />}</ResourceState></div>}
  </div>;
}

export function ResourceUsagePage({ user, navigate }: { user: User; navigate: (path: string) => void }) {
  const selectedTab = () => new URLSearchParams(location.hash.split("?")[1] ?? "").get("view") === "exports" ? "exports" : "calls";
  const [tab, setTab] = useState(selectedTab);
  const [createRequested, setCreateRequested] = useState(false);
  const allowed = !!user.capabilities?.includes("exports.create");
  useEffect(() => { const changed = () => setTab(selectedTab()); window.addEventListener("hashchange",changed); return () => window.removeEventListener("hashchange",changed); },[]);
  const changeTab = (value: string) => { setCreateRequested(false); setTab(value); navigate(value === "exports" ? "usage?view=exports" : "usage"); };
  const navigation = <ResourceTabs selected={tab} onChange={changeTab} entries={[{value:"calls",label:"调用记录"},...(allowed ? [{value:"exports",label:"我的导出"}] : [])]} />;
  return <div className="resource-page resource-usage" data-testid={`resource-${tab}`}>
    {tab === "exports" && allowed ? <TenantExportsPanel user={user} designMode navigation={navigation} initialCreate={createRequested} /> : <CallRecords navigation={navigation} createExport={allowed ? () => { setCreateRequested(true); setTab("exports"); navigate("usage?view=exports"); } : undefined} />}
  </div>;
}
function CallRecords({ navigation, createExport }: { navigation: ReactNode; createExport?: () => void }) {
  const resource = useResource<Items<Call> & { next_cursor: string | null }>("/usage/calls?limit=100");
  const [search, setSearch] = useState("");
  const [status, setStatus] = useState("all");
  const [detail, setDetail] = useState<Call | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState("");
  const lock = useRef(false);
  const generation = useRef(0);
  useEffect(() => () => { generation.current++; }, []);
  const items = resource.data?.items.filter(call => `${call.id} ${call.model} ${call.agent_name} ${call.user_email}`.toLowerCase().includes(search.toLowerCase()) && (status === "all" || status === call.status)) ?? [];
  async function loadMore() {
    if (!resource.data?.next_cursor || lock.current) return;
    const current = generation.current;
    const scope = getScopeRevision();
    lock.current = true; setLoadingMore(true); setMoreError("");
    try {
      const result = await api<Items<Call> & { next_cursor: string | null }>(`/usage/calls?limit=100&cursor=${encodeURIComponent(resource.data.next_cursor)}`);
      if (current !== generation.current || !scopeIsCurrent(scope)) return;
      resource.setData(previous => ({ items: [...(previous?.items ?? []), ...result.items.filter(item => !previous?.items.some(old => old.id === item.id))], next_cursor: result.next_cursor }));
    } catch(error) { if (current === generation.current && scopeIsCurrent(scope)) setMoreError((error as Error).message); }
    finally { lock.current = false; if(current === generation.current) setLoadingMore(false); }
  }
  const refresh = () => { generation.current++; setLoadingMore(false); setMoreError(""); void resource.reload(); };
  function downloadCurrent() {
    const rows = [["时间","模型","Agent","用户","状态","输入 Token","输出 Token","客户费用 USD"], ...items.map(call => [call.created_at,call.model,call.agent_name,call.user_email,call.status,call.input_tokens,call.output_tokens,call.cost])];
    const csv = "\uFEFF" + rows.map(row => row.map(value => `"${String(value).replace(/^(?:\s*[=+\-@]|[\t\r\n])/,"'$&").replaceAll('"','""')}"`).join(",")).join("\r\n");
    const url = URL.createObjectURL(new Blob([csv],{type:"text/csv;charset=utf-8"})); const anchor=document.createElement("a"); anchor.href=url; anchor.download="agent-usage-current.csv"; anchor.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  const settled = (call: Call) => ["confirmed","written_off"].includes(call.status);
  return <>
    <ResourceHeader title="调用统计与导出" description="查看当前组织的模型调用流水与资源审计，支持异步导出明细数据" action={createExport && <Button onClick={createExport}><ResourceIcon file="a4b8c" />创建导出</Button>} />
    <div className="resource-usage-tabs">{navigation}<ResourceSearch value={search} onChange={setSearch} placeholder="筛选已加载调用..." /></div>
    <div className="resource-table-wrap"><ResourceState loading={resource.loading} error={resource.error} retry={refresh}>
      {items.length ? <table className="resource-table resource-call-table"><colgroup>{[22,18,13,15,12,12,8].map((width,index) => <col key={index} style={{width:`${width}%`}} />)}</colgroup><thead><tr><th>Agent / Call ID</th><th>模型</th><th>状态</th><th>输入 / 输出 Tokens</th><th>客户费用 (USD)</th><th>创建时间</th><th>详情</th></tr></thead><tbody>{items.map(call => <tr key={call.id}><td><strong>{call.agent_name || "—"}</strong><small className="resource-mono">{call.id}</small></td><td><code className="resource-model-tag">{call.model}</code></td><td><ResourceStatus status={call.status} /></td><td>{settled(call) ? <span className="resource-mono">{call.input_tokens} <span className="resource-muted">/</span> {call.output_tokens}</span> : <span className="resource-muted">待核实 / 待核实</span>}</td><td>{settled(call) ? <b className="resource-mono">{resourceMoney(call.cost)}</b> : <span className="resource-muted">待核实</span>}</td><td className="resource-muted">{resourceTime(call.created_at)}</td><td><button className="resource-call-detail" onClick={() => setDetail(call)}>查看</button></td></tr>)}</tbody></table> : <Empty title={search || status !== "all" ? "当前已加载记录无匹配" : "暂无调用明细"} description="实际调用发生后显示用量、费用与结算状态。" />}
    </ResourceState><div className="resource-table-footer"><details className="resource-call-options"><summary>已加载 {resource.data?.items.length ?? 0} 条调用记录</summary><div><label>调用状态<select value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option>{Array.from(new Set(resource.data?.items.map(call => call.status))).map(value => <option key={value} value={value}>{value}</option>)}</select></label><button onClick={refresh}>刷新调用记录</button><button onClick={downloadCurrent} disabled={!items.length}>导出当前筛选结果 CSV</button></div></details><Button variant="secondary" disabled={!resource.data?.next_cursor || resource.loading} busy={loadingMore} onClick={() => void loadMore()}>加载更多</Button></div>{moreError && <ErrorState message={moreError} retry={() => void loadMore()} />}</div>
    <p className="resource-footnote">* 计量未确认时显示待核实。</p>
    {detail && <Modal title="调用详情" description="仅展示当前权限下的调用元数据。" close={() => setDetail(null)}><dl className="resource-call-metadata">{[["Call ID",detail.id],["Agent",detail.agent_name],["模型",detail.model],["用户",detail.user_email],["Run ID",detail.run_id],["状态",detail.status],["输入 / 输出 Token",settled(detail) ? `${detail.input_tokens} / ${detail.output_tokens}` : "待核实"],["客户费用",settled(detail) ? resourceMoney(detail.cost) : "待核实"],["创建时间",resourceTime(detail.created_at,true)],["错误",detail.error || "无"]].map(([label,value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl></Modal>}
  </>;
}
