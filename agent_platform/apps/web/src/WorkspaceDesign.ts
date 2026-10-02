import "./workspace-design.css";

export const workspaceTitles: Record<string, string> = {
  overview: "概览", runs: "我的运行记录",
};
const menuIcons: Record<string, string> = {
  overview: "a7ba3", agents: "b8aca", playground: "03f57", runs: "6fe0f",
  models: "c89cf", usage: "4c841", billing: "b9344", users: "cebbe",
  quotas: "7b157", audit: "147bb", "support-access": "f21af",
};
export function workspaceIcons(route: string) {
  return { ...menuIcons, [route]: route === "overview" ? "891f7" : "f738e" };
}
