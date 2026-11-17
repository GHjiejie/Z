import { ApiError, aborted } from "./errors";
import {
  decodeRun,
  decodeRuns,
  decodeOptions,
  readRunStream,
  type ChatRun,
  type RunEvent,
  type ToolOption,
} from "./chatRuns";
import {
  chatFailure,
  readChatStream,
  type ChatMessageInput,
  type ChatStreamResult,
} from "./chatStream";
export { ApiError, errorMessage, aborted } from "./errors";

export type RoleCode = string;
export type UserStatus = "active" | "disabled";
export interface User {
  id: string;
  username: string;
  display_name: string;
  email: string | null;
  roles: RoleCode[];
  status: UserStatus;
  must_change_password: boolean;
  version: number;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
}
export interface UserRecord {
  user: User;
  etag: string;
}
export interface UserPage {
  items: User[];
  total: number;
  page: number;
  page_size: number;
}
export interface LoginResult {
  session_token: string;
  expires_in: number;
  auth_scope: "full";
  user: User;
}
export interface RoleOption {
  code: RoleCode;
  display_name: string;
  description: string;
}
export interface RoleDefinition extends RoleOption {
  permissions: string[];
  built_in: boolean;
  user_count: number;
  version: number;
  created_at: string;
  updated_at: string;
}
export interface RoleRecord {
  role: RoleDefinition;
  etag: string;
}
export interface RoleDraft {
  code: string;
  display_name: string;
  description: string;
  permissions: string[];
}
export interface PermissionDefinition {
  code: string;
  display_name: string;
  resource: string;
}
export interface OwnPermissions {
  permissions: string[];
  role_details: RoleOption[];
}
export type AgentToolState =
  | "not_granted"
  | "missing_business_permission"
  | "ready"
  | "unavailable"
  | "account_disabled";
export interface AgentToolGrant {
  operation_id: string;
  tool_name: string | null;
  display_name: string;
  resource: string;
  effect: string;
  enabled: boolean;
  rbac_allowed: boolean;
  available: boolean;
  state: AgentToolState;
  required_permissions: { all: string[]; any: string[] };
}
export interface UserAgentToolGrants {
  user_id: string;
  version: number;
  catalog_revision: string;
  can_manage: boolean;
  items: AgentToolGrant[];
}
export interface UserQuery {
  page: number;
  page_size: number;
  q: string;
  status: "" | UserStatus;
  role: "" | RoleCode;
  sort_by: "created_at" | "username" | "last_login_at";
  sort_order: "asc" | "desc";
}
export interface UserDraft {
  username: string;
  display_name: string;
  email: string | null;
  password: string;
  roles: RoleCode[];
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function object(value: unknown): Record<string, unknown> {
  if (!isObject(value)) throw new Error("Invalid object");
  return value;
}
function text(value: unknown): string {
  if (typeof value !== "string") throw new Error("Invalid string");
  return value;
}
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value))
    throw new Error("Invalid number");
  return value;
}
function boolean(value: unknown): boolean {
  if (typeof value !== "boolean") throw new Error("Invalid boolean");
  return value;
}
function decodeAgentToolGrants(value: unknown): UserAgentToolGrants {
  const data = object(value);
  if (!Array.isArray(data.items)) throw new Error("Invalid tool list");
  const states: AgentToolState[] = [
    "not_granted",
    "missing_business_permission",
    "ready",
    "unavailable",
    "account_disabled",
  ];
  return {
    user_id: text(data.user_id),
    version: number(data.version),
    catalog_revision: text(data.catalog_revision),
    can_manage: boolean(data.can_manage),
    items: data.items.map((value: unknown) => {
      const item = object(value);
      const required = object(item.required_permissions);
      const state = states.find((state) => state === item.state);
      if (!state) throw new Error("Invalid tool state");
      return {
        operation_id: text(item.operation_id),
        tool_name: nullableText(item.tool_name),
        display_name: text(item.display_name),
        resource: text(item.resource),
        effect: text(item.effect),
        enabled: boolean(item.enabled),
        rbac_allowed: boolean(item.rbac_allowed),
        available: boolean(item.available),
        state,
        required_permissions: {
          all: strings(required.all),
          any: strings(required.any),
        },
      };
    }),
  };
}
function strings(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("Invalid array");
  return value.map(text);
}
function role(value: unknown): RoleCode {
  if (typeof value !== "string" || !/^[a-z][a-z0-9_-]{2,63}$/.test(value))
    throw new Error("Invalid role");
  return value;
}
function decodeRoleOption(value: unknown): RoleOption {
  const data = object(value);
  return {
    code: role(data.code),
    display_name: text(data.display_name),
    description: text(data.description),
  };
}
function decodeRole(value: unknown): RoleDefinition {
  const data = object(value);
  if (typeof data.built_in !== "boolean") throw new Error("Invalid role");
  return {
    ...decodeRoleOption(data),
    permissions: strings(data.permissions),
    built_in: data.built_in,
    user_count: number(data.user_count),
    version: number(data.version),
    created_at: text(data.created_at),
    updated_at: text(data.updated_at),
  };
}
function roles(value: unknown): RoleCode[] {
  if (!Array.isArray(value) || value.length === 0)
    throw new Error("Invalid roles");
  return value.map(role);
}
function status(value: unknown): UserStatus {
  if (value !== "active" && value !== "disabled")
    throw new Error("Invalid status");
  return value;
}
function nullableText(value: unknown): string | null {
  return value === null ? null : text(value);
}
function decodeUser(value: unknown): User {
  const data = object(value);
  if (typeof data.must_change_password !== "boolean")
    throw new Error("Invalid boolean");
  return {
    id: text(data.id),
    username: text(data.username),
    display_name: text(data.display_name),
    email: nullableText(data.email),
    roles: roles(data.roles),
    status: status(data.status),
    must_change_password: data.must_change_password,
    version: number(data.version),
    last_login_at: nullableText(data.last_login_at),
    created_at: text(data.created_at),
    updated_at: text(data.updated_at),
  };
}

export class ApiClient {
  constructor(
    private readonly token: string | null,
    private readonly onExpired: () => void,
    private readonly onPermissionsChanged: () => void,
    private readonly isCurrentSession: () => boolean,
  ) {}

  private requireCurrentSession(path: string): void {
    if (path !== "/auth/login" && !this.isCurrentSession()) {
      throw new DOMException("The session has changed", "AbortError");
    }
  }

  private async request(
    path: string,
    options: RequestInit = {},
    prefix = "/api/v1",
  ): Promise<Response> {
    this.requireCurrentSession(path);
    const headers = new Headers(options.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    if (options.body) headers.set("Content-Type", "application/json");
    let response: Response;
    try {
      response = await fetch(`${prefix}${path}`, {
        ...options,
        headers,
        credentials: "omit",
      });
    } catch (error) {
      if (aborted(error)) throw error;
      throw new ApiError("无法连接到服务，请检查连接后重试", "NETWORK_ERROR");
    }
    this.requireCurrentSession(path);
    return response;
  }

  private async send(
    path: string,
    options: RequestInit = {},
  ): Promise<{ data: unknown; etag: string | null }> {
    const response = await this.request(path, options);
    if (response.status === 204) return { data: null, etag: null };
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      this.requireCurrentSession(path);
      throw new ApiError(
        "服务返回的数据不完整，请重试",
        "INVALID_RESPONSE",
        response.status,
      );
    }
    this.requireCurrentSession(path);
    if (!response.ok) {
      const failure =
        isObject(payload) && isObject(payload.error) ? payload.error : {};
      const code =
        typeof failure.code === "string" ? failure.code : "REQUEST_FAILED";
      if (response.status === 401 && path !== "/auth/login") this.onExpired();
      if (
        code === "PERMISSION_DENIED" &&
        path !== "/users/me" &&
        path !== "/users/me/permissions"
      )
        this.onPermissionsChanged();
      const details = Array.isArray(failure.details)
        ? failure.details.flatMap((entry: unknown) => {
            if (
              isObject(entry) &&
              typeof entry.field === "string" &&
              typeof entry.message === "string"
            ) {
              return [{ field: entry.field, message: entry.message }];
            }
            return [];
          })
        : [];
      throw new ApiError(
        typeof failure.message === "string"
          ? failure.message
          : "操作失败，请稍后重试",
        code,
        response.status,
        details,
        Number(response.headers.get("Retry-After")) || 0,
      );
    }
    if (!isObject(payload) || !("data" in payload)) {
      throw new ApiError("服务返回的数据不完整，请重试", "INVALID_RESPONSE");
    }
    return { data: payload.data, etag: response.headers.get("ETag") };
  }

  private chatError(error: unknown): never {
    this.requireCurrentSession("/v1/chat/completions");
    if (error instanceof ApiError) {
      if (error.status === 401 || error.code === "UNAUTHENTICATED")
        this.onExpired();
      if (error.status === 403 || error.code === "PERMISSION_DENIED")
        this.onPermissionsChanged();
    }
    throw error;
  }

  async chatModels(signal: AbortSignal): Promise<string[]> {
    try {
      const response = await this.request("/v1/models", { signal }, "");
      const payload: unknown = await response.json();
      this.requireCurrentSession("/v1/models");
      if (!response.ok) throw chatFailure(payload, response.status);
      return this.decode(payload, (value) => {
        const data = object(value);
        if (data.object !== "list" || !Array.isArray(data.data))
          throw new Error("Invalid models");
        return data.data.map((item: unknown) => text(object(item).id));
      });
    } catch (error) {
      return this.chatError(error);
    }
  }

  async chatToolCount(signal: AbortSignal): Promise<number> {
    try {
      const response = await this.request("/agent-tools", { signal }, "");
      const payload: unknown = await response.json();
      this.requireCurrentSession("/agent-tools");
      if (!response.ok) throw chatFailure(payload, response.status);
      return this.decode(payload, (value) => {
        const catalog = object(value);
        if (!Array.isArray(catalog.tools)) throw new Error("Invalid tools");
        return catalog.tools
          .map((item: unknown) => text(object(item).name))
          .filter((name) => name !== "agent_users_auth_login").length;
      });
    } catch (error) {
      return this.chatError(error);
    }
  }

  async streamChat(
    model: string,
    messages: ChatMessageInput[],
    signal: AbortSignal,
    onText: (text: string) => void,
  ): Promise<ChatStreamResult> {
    try {
      const response = await this.request(
        "/v1/chat/completions",
        {
          method: "POST",
          signal,
          body: JSON.stringify({
            model,
            messages,
            stream: true,
            stream_options: { include_usage: true },
          }),
        },
        "",
      );
      if (!response.ok) {
        const payload: unknown = await response.json();
        this.requireCurrentSession("/v1/chat/completions");
        throw chatFailure(payload, response.status);
      }
      if (
        !response.body ||
        !response.headers.get("Content-Type")?.includes("text/event-stream")
      )
        throw new ApiError("服务未返回流式回复", "INVALID_RESPONSE");
      const result = await readChatStream(response.body, signal, (text) => {
        this.requireCurrentSession("/v1/chat/completions");
        onText(text);
      });
      this.requireCurrentSession("/v1/chat/completions");
      return result;
    } catch (error) {
      return this.chatError(error);
    }
  }

  async chatRuns(
    conversationId: string,
    signal: AbortSignal,
  ): Promise<ChatRun[]> {
    const result = await this.send(
      `/chat/runs?conversation_id=${encodeURIComponent(conversationId)}`,
      { signal },
    );
    return this.decode(result.data, decodeRuns);
  }

  async chatRun(runId: string, signal?: AbortSignal): Promise<ChatRun> {
    const result = await this.send(`/chat/runs/${encodeURIComponent(runId)}`, {
      signal,
    });
    return this.decode(result.data, decodeRun);
  }

  async cancelChatRun(runId: string): Promise<ChatRun> {
    const result = await this.send(
      `/chat/runs/${encodeURIComponent(runId)}/cancel`,
      { method: "POST" },
    );
    return this.decode(result.data, decodeRun);
  }

  async refreshInteraction(
    runId: string,
    interactionId: string,
    version: number,
    requestId: string,
  ): Promise<ChatRun> {
    const result = await this.send(
      `/chat/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/refresh`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_version: version,
          request_id: requestId,
        }),
      },
    );
    return this.decode(result.data, decodeRun);
  }

  async interactionOptions(
    runId: string,
    interactionId: string,
    field: string,
    query: string,
    signal: AbortSignal,
  ): Promise<ToolOption[]> {
    const params = new URLSearchParams({ field, q: query });
    const result = await this.send(
      `/chat/runs/${encodeURIComponent(runId)}/interactions/${encodeURIComponent(interactionId)}/options?${params}`,
      { signal },
    );
    return this.decode(result.data, decodeOptions);
  }

  async streamRun(
    path: string,
    payload: object,
    signal: AbortSignal,
    onEvent: (event: RunEvent) => void,
  ): Promise<void> {
    try {
      const response = await this.request(path, {
        method: "POST",
        signal,
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        const raw: unknown = await response.json();
        this.requireCurrentSession(path);
        const failure = isObject(raw) && isObject(raw.error) ? raw.error : {};
        const details = Array.isArray(failure.details)
          ? failure.details.flatMap((item: unknown) =>
              isObject(item) &&
              typeof item.field === "string" &&
              typeof item.message === "string"
                ? [{ field: item.field, message: item.message }]
                : [],
            )
          : [];
        throw new ApiError(
          typeof failure.message === "string" ? failure.message : "操作失败",
          typeof failure.code === "string" ? failure.code : "REQUEST_FAILED",
          response.status,
          details,
        );
      }
      if (
        !response.body ||
        !response.headers.get("Content-Type")?.includes("text/event-stream")
      )
        throw new ApiError("服务未返回任务流", "INVALID_RESPONSE");
      await readRunStream(response.body, signal, (event) => {
        this.requireCurrentSession(path);
        onEvent(event);
      });
    } catch (error) {
      return this.chatError(error);
    }
  }

  private decode<T>(data: unknown, decoder: (value: unknown) => T): T {
    try {
      return decoder(data);
    } catch {
      throw new ApiError(
        "服务返回的数据不完整，请刷新后重试",
        "INVALID_RESPONSE",
      );
    }
  }
  private record(result: { data: unknown; etag: string | null }): UserRecord {
    const user = this.decode(result.data, decodeUser);
    if (!result.etag || !/^"v[1-9][0-9]*"$/.test(result.etag)) {
      throw new ApiError("无法读取用户的最新版本，请重试", "INVALID_RESPONSE");
    }
    return { user, etag: result.etag };
  }
  async login(username: string, password: string): Promise<LoginResult> {
    const result = await this.send("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    return this.decode(result.data, (value) => {
      const data = object(value);
      if (data.auth_scope !== "full") throw new Error("Invalid scope");
      return {
        session_token: text(data.session_token),
        expires_in: number(data.expires_in),
        auth_scope: data.auth_scope,
        user: decodeUser(data.user),
      };
    });
  }
  async logout(): Promise<void> {
    await this.send("/auth/logout", { method: "POST" });
  }
  async me(signal?: AbortSignal): Promise<UserRecord> {
    return this.record(await this.send("/users/me", { signal }));
  }
  async permissions(signal?: AbortSignal): Promise<OwnPermissions> {
    const result = await this.send("/users/me/permissions", { signal });
    return this.decode(result.data, (value) => {
      const data = object(value);
      if (!Array.isArray(data.role_details)) throw new Error("Invalid roles");
      return {
        permissions: strings(data.permissions),
        role_details: data.role_details.map(decodeRoleOption),
      };
    });
  }
  async roles(signal?: AbortSignal): Promise<RoleDefinition[]> {
    const result = await this.send("/roles", { signal });
    return this.decode(result.data, (value) => {
      if (!Array.isArray(value)) throw new Error("Invalid array");
      return value.map(decodeRole);
    });
  }
  async roleOptions(signal?: AbortSignal): Promise<RoleOption[]> {
    const result = await this.send("/role-options", { signal });
    return this.decode(result.data, (value) => {
      if (!Array.isArray(value)) throw new Error("Invalid roles");
      return value.map(decodeRoleOption);
    });
  }
  async permissionCatalog(
    signal?: AbortSignal,
  ): Promise<PermissionDefinition[]> {
    const result = await this.send("/permissions", { signal });
    return this.decode(result.data, (value) => {
      if (!Array.isArray(value)) throw new Error("Invalid permissions");
      return value.map((entry) => {
        const data = object(entry);
        return {
          code: text(data.code),
          display_name: text(data.display_name),
          resource: text(data.resource),
        };
      });
    });
  }
  private roleRecord(result: {
    data: unknown;
    etag: string | null;
  }): RoleRecord {
    const value = this.decode(result.data, decodeRole);
    if (!result.etag || !/^"v[1-9][0-9]*"$/.test(result.etag))
      throw new ApiError("无法读取角色的最新版本，请重试", "INVALID_RESPONSE");
    return { role: value, etag: result.etag };
  }
  async getRole(code: string, signal?: AbortSignal): Promise<RoleRecord> {
    return this.roleRecord(
      await this.send(`/roles/${encodeURIComponent(code)}`, { signal }),
    );
  }
  async createRole(draft: RoleDraft): Promise<RoleRecord> {
    return this.roleRecord(
      await this.send("/roles", {
        method: "POST",
        body: JSON.stringify(draft),
      }),
    );
  }
  async updateRole(record: RoleRecord, draft: RoleDraft): Promise<RoleRecord> {
    return this.roleRecord(
      await this.send(`/roles/${encodeURIComponent(record.role.code)}`, {
        method: "PATCH",
        headers: { "If-Match": record.etag },
        body: JSON.stringify({
          display_name: draft.display_name,
          description: draft.description,
          permissions: draft.permissions,
        }),
      }),
    );
  }
  async deleteRole(record: RoleRecord): Promise<void> {
    await this.send(`/roles/${encodeURIComponent(record.role.code)}`, {
      method: "DELETE",
      headers: { "If-Match": record.etag },
    });
  }
  async users(
    query: Partial<UserQuery>,
    signal?: AbortSignal,
  ): Promise<UserPage> {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(query))
      if (value !== "") search.set(key, String(value));
    const result = await this.send(`/users?${search}`, { signal });
    return this.decode(result.data, (value) => {
      const data = object(value);
      if (!Array.isArray(data.items)) throw new Error("Invalid array");
      return {
        items: data.items.map(decodeUser),
        total: number(data.total),
        page: number(data.page),
        page_size: number(data.page_size),
      };
    });
  }
  async getUser(id: string, signal?: AbortSignal): Promise<UserRecord> {
    return this.record(
      await this.send(`/users/${encodeURIComponent(id)}`, { signal }),
    );
  }
  async userAgentTools(
    id: string,
    signal?: AbortSignal,
  ): Promise<UserAgentToolGrants> {
    const result = await this.send(
      `/users/${encodeURIComponent(id)}/agent-tools`,
      { signal },
    );
    return this.decode(result.data, decodeAgentToolGrants);
  }
  async updateUserAgentTools(
    id: string,
    current: UserAgentToolGrants,
    changes: { operation_id: string; enabled: boolean }[],
    signal?: AbortSignal,
  ): Promise<UserAgentToolGrants> {
    const result = await this.send(
      `/users/${encodeURIComponent(id)}/agent-tools`,
      {
        method: "PATCH",
        signal,
        body: JSON.stringify({
          expected_version: current.version,
          catalog_revision: current.catalog_revision,
          changes,
        }),
      },
    );
    return this.decode(result.data, decodeAgentToolGrants);
  }
  async createUser(draft: UserDraft): Promise<UserRecord> {
    return this.record(
      await this.send("/users", {
        method: "POST",
        body: JSON.stringify(draft),
      }),
    );
  }
  async profile(
    record: UserRecord,
    draft: { display_name: string; email: string | null },
    self = false,
  ): Promise<UserRecord> {
    return this.record(
      await this.send(
        self ? "/users/me" : `/users/${encodeURIComponent(record.user.id)}`,
        {
          method: "PATCH",
          body: JSON.stringify(draft),
          headers: { "If-Match": record.etag },
        },
      ),
    );
  }
  async setRoles(
    record: UserRecord,
    nextRoles: RoleCode[],
  ): Promise<UserRecord> {
    return this.record(
      await this.send(`/users/${encodeURIComponent(record.user.id)}/roles`, {
        method: "PUT",
        body: JSON.stringify({ roles: nextRoles }),
        headers: { "If-Match": record.etag },
      }),
    );
  }
  async setStatus(
    record: UserRecord,
    nextStatus: UserStatus,
  ): Promise<UserRecord> {
    return this.record(
      await this.send(`/users/${encodeURIComponent(record.user.id)}/status`, {
        method: "PUT",
        body: JSON.stringify({ status: nextStatus }),
        headers: { "If-Match": record.etag },
      }),
    );
  }
  async removeUser(record: UserRecord): Promise<void> {
    await this.send(`/users/${encodeURIComponent(record.user.id)}`, {
      method: "DELETE",
      headers: { "If-Match": record.etag },
    });
  }
  async resetPassword(record: UserRecord, password: string): Promise<void> {
    await this.send(`/users/${encodeURIComponent(record.user.id)}/password`, {
      method: "PUT",
      body: JSON.stringify({ new_password: password }),
      headers: { "If-Match": record.etag },
    });
  }
  async changePassword(
    record: UserRecord,
    current: string,
    next: string,
  ): Promise<void> {
    await this.send("/users/me/password", {
      method: "PUT",
      headers: { "If-Match": record.etag },
      body: JSON.stringify({ current_password: current, new_password: next }),
    });
  }
}
