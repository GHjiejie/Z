import type { ReactNode } from "react";
import "./organization-design.css";

export const organizationTitles: Record<string, string> = {
  users: "成员与邀请", quotas: "组织配额", audit: "审计日志", "support-access": "Owner支持审批",
};
// Original icon variants exported from each organization's Figma frame.
const routes = ["overview", "agents", "playground", "runs", "models", "usage", "billing", "users", "quotas", "audit", "support-access"];
const variants: Record<string, string[]> = {
  users: ["c5523", "bf70e", "5b6cf", "81e44", "cf2f4", "1f24a", "daa32", "02a8d", "5d070", "555a1", "1f4f4"],
  quotas: ["59475", "65527", "b8f9d", "1f4f4", "a0877", "dbd02", "1c291", "ea82a", "81825", "63173", "a8336"],
  audit: ["5d605", "f704f", "39b91", "7422d", "fa4bf", "3065e", "1372c", "2384c", "d4ee5", "148e4", "472a3"],
  "support-access": ["5d605", "f704f", "39b91", "7422d", "fa4bf", "3065e", "1372c", "2384c", "d4ee5", "11b7d", "de87d"],
};
export function organizationIcons(route: string) {
  return Object.fromEntries(routes.map((key, index) => [key, variants[route][index]]));
}
export function OrganizationIcon({ file }: { file: string }) {
  return <img className="organization-icon" alt="" src={`/organization-design/${file}.svg`} />;
}
export function OrganizationHeading({ title, description, children }: { title: string; description: string; children?: ReactNode }) {
  return <header className="organization-heading"><div><h1>{title === "Owner支持审批" ? <><span>Owner</span><span>支持审批</span></> : title}</h1><p>{description}</p></div><div className="organization-actions">{children}</div></header>;
}
export function OrganizationSearch({ value, change, placeholder, file = "02b5f" }: { value: string; change: (value: string) => void; placeholder: string; file?: string }) {
  return <label className="organization-search"><OrganizationIcon file={file} /><input aria-label={placeholder} placeholder={placeholder} value={value} onChange={event => change(event.target.value)} /></label>;
}
export function OrganizationFooter({ children, note }: { children: ReactNode; note?: string }) {
  return <footer className="organization-table-footer"><span>{children}</span>{note && <span>{note}</span>}</footer>;
}
export function OrganizationTabs({ tabs, selected, change }: { tabs: { id: string; title: string; suffix?: ReactNode }[]; selected: string; change: (id: string) => void }) {
  return <div className="organization-tabs" role="tablist">{tabs.map(tab => <button key={tab.id} role="tab" aria-selected={selected === tab.id} aria-controls={`organization-${tab.id}`} id={`tab-${tab.id}`} onClick={() => change(tab.id)}>{tab.title}{tab.suffix}</button>)}</div>;
}

export function organizationTime(value: string | number, short = false) {
  const date = new Date(typeof value === "number" ? value * 1000 : value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  if (Number.isNaN(date.getTime())) return "—";
  const pad = (number: number) => String(number).padStart(2,"0");
  return `${short ? "" : `${date.getFullYear()}-`}${pad(date.getMonth()+1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
