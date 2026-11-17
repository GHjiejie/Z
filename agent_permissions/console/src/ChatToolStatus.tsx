import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { aborted, type ApiClient, type User } from "./api";
import { ErrorBanner, Spinner } from "./components";

type ToolStatus =
  | { state: "loading" }
  | { state: "ready"; count: number }
  | { state: "error"; error: unknown };

export function ChatToolStatus({
  api,
  user,
  permissions,
}: {
  api: ApiClient;
  user: User;
  permissions: string[];
}) {
  const [status, setStatus] = useState<ToolStatus>({ state: "loading" });
  const [revision, setRevision] = useState(0);
  const permissionKey = permissions.join("|");
  const canEditGrants =
    permissions.includes("user.read") &&
    permissions.includes("user.agent_tools.manage");

  useEffect(() => {
    const controller = new AbortController();
    setStatus({ state: "loading" });
    void api
      .chatToolCount(controller.signal)
      .then((count) => {
        if (!controller.signal.aborted) setStatus({ state: "ready", count });
      })
      .catch((error: unknown) => {
        if (!controller.signal.aborted && !aborted(error))
          setStatus({ state: "error", error });
      });
    return () => controller.abort();
  }, [api, user.id, permissionKey, revision]);

  useEffect(() => {
    const refresh = () => setRevision((value) => value + 1);
    window.addEventListener("focus", refresh);
    return () => window.removeEventListener("focus", refresh);
  }, []);

  return (
    <div
      className={`chat-tool-status${status.state === "ready" && !status.count ? " is-empty" : status.state === "error" ? " has-error" : ""}`}
      aria-label="Agent 工具状态"
    >
      <div className="chat-tool-status-copy" role="status">
        {status.state === "loading" ? (
          <Spinner label="正在读取可用工具" />
        ) : status.state === "error" ? (
          <ErrorBanner
            error={status.error}
            onRetry={() => setRevision((value) => value + 1)}
          />
        ) : status.count ? (
          <span>可用业务工具：{status.count} 个</span>
        ) : (
          <>
            <strong>当前没有可用的 Agent 业务工具</strong>
            <p>
              普通聊天可用；查询、参数表单和业务操作需要工具授权及对应业务权限。
              {canEditGrants
                ? "请在用户管理中，通过用户的“更多操作 → Agent 工具授权”开启所需工具。"
                : "请联系管理员核对工具授权和业务权限。"}
            </p>
          </>
        )}
      </div>
      <button
        className="icon-button"
        type="button"
        aria-label="刷新工具状态"
        title="刷新工具状态"
        disabled={status.state === "loading"}
        onClick={() => setRevision((value) => value + 1)}
      >
        <RefreshCw size={15} />
      </button>
    </div>
  );
}
