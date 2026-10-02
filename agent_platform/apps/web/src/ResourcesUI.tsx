import type { ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { tenantLink } from "./api";

export function ResourceIcon({ file }: { file: string }) {
  return <img className="resource-icon" src={`/resource-design/${file}.svg`} alt="" />;
}

const labels: Record<string, string> = {
  overview: "概览 Dashboard", agents: "Agent管理", playground: "对话实验室", runs: "我的运行记录",
  models: "模型策略", usage: "调用统计与导出", billing: "费用中心", users: "成员与邀请",
  quotas: "组织配额", audit: "审计日志", "support-access": "Owner支持审批",
};
const materialIcons: Record<string, string> = {
  overview: "9b916", agents: "c1fba", playground: "38d64", runs: "e9240", models: "f0196",
  usage: "700e3", billing: "7ffed", users: "615bd", quotas: "f43bb", audit: "bde47", "support-access": "ecb3e",
};
const usageIcons: Record<string, string> = {
  overview: "4ec75", agents: "2621e", playground: "97f82", runs: "f80f7", models: "25db0",
  usage: "1015e", billing: "6428e", users: "e612e", quotas: "5cf1b", audit: "f1a61", "support-access": "ecb66",
};
export function ResourcesSidebar({ items, route, collapsed, toggle }: {
  items: { route: string; title: string; group: string; icon: LucideIcon }[];
  route: string; collapsed: boolean; toggle: () => void;
}) {
  const usage = route === "usage";
  const icons: Record<string, string> = { ...(usage ? usageIcons : materialIcons), ...(route === "models" ? { models: "80533" } : route === "billing" ? { billing: "49d0f" } : {}) };
  return <>
    <div className="resource-brand-row">
      {collapsed ? <button className="resource-logo-button" aria-label="展开侧栏" onClick={toggle}><span className="resource-logo">Z</span></button> : <>
        <a className="resource-brand" href={tenantLink("overview")}><span className="resource-logo">Z</span><span>Z Agent</span></a>
        <button className="resource-collapse" aria-label="收起侧栏" onClick={toggle}><ResourceIcon file="f4b93" /></button>
      </>}
    </div>
    <nav className="resource-nav" aria-label="主导航">
      {["工作空间", "资源与费用", "组织管理"].map(group => {
        const entries = items.filter(item => item.group === group);
        return entries.length ? <div className="resource-nav-group" key={group}>
          {!collapsed && <div className="resource-nav-label">{group}</div>}
          {entries.map(item => <div key={item.route}>
            <a className={`resource-nav-link ${item.route === route ? "selected" : ""}`} href={tenantLink(item.route)} aria-current={item.route === route ? "page" : undefined} aria-label={collapsed ? labels[item.route] : undefined}>
              {icons[item.route] ? <ResourceIcon file={icons[item.route]} /> : <item.icon size={16} />}
              {!collapsed && <span>{labels[item.route] ?? item.title}</span>}
            </a>
            {!collapsed && item.route === "playground" && <div className="resource-subnav">
              <a href={tenantLink("playground")}>{usage ? <span className="resource-plus">+</span> : <ResourceIcon file={route === "billing" ? "5de3b" : "1c0cd"} />}<span>新建会话</span></a>
              <a href={tenantLink("playground")}>{!usage && <ResourceIcon file="04623" />}<span>会话记录</span></a>
              <a href={tenantLink("playground")}>{!usage && <ResourceIcon file="58f93" />}<span>运行轨迹</span><small>当前会话</small></a>
            </div>}
          </div>)}
        </div> : null;
      })}
    </nav>
  </>;
}

export function ResourceHeader({ title, description, action }: { title: string; description: string; action?: ReactNode }) {
  return <header className="resource-heading"><div><h1>{title}</h1><p>{description}</p></div>{action}</header>;
}
export function ResourceSearch({ value, onChange, placeholder, icon = "3457b" }: { value: string; onChange: (value: string) => void; placeholder: string; icon?: string }) {
  return <label className="resource-search"><ResourceIcon file={icon} /><input aria-label={placeholder} placeholder={placeholder} value={value} onChange={event => onChange(event.target.value)} /></label>;
}
export function ResourceStatus({ status, children }: { status: string; children?: ReactNode }) {
  return <span className={`resource-status status-${status}`}><i />{children ?? status}</span>;
}
export function ResourceTabs({ entries, selected, onChange }: { entries: { value: string; label: ReactNode }[]; selected: string; onChange: (value: string) => void }) {
  return <div className="resource-tabs" role="tablist">{entries.map(entry => <button key={entry.value} role="tab" aria-selected={selected === entry.value} className={selected === entry.value ? "selected" : ""} onClick={() => onChange(entry.value)}>{entry.label}</button>)}</div>;
}
export function resourceTime(value: string | number, full = false) {
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.valueOf())) return "—";
  const parts = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(date);
  const p = (type: string) => parts.find(part => part.type === type)?.value;
  return `${full ? `${p("year")}-` : ""}${p("month")}-${p("day")} ${p("hour")}:${p("minute")}:${p("second")}`;
}
export const resourceMoney = (value: string, digits = 4) => new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: digits, maximumFractionDigits: digits }).format(Number(value));
