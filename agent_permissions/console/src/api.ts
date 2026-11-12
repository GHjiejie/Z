export type RoleCode = "admin" | "member";
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
  auth_scope: "full" | "password_change";
  user: User;
}
export interface RoleDefinition {
  code: RoleCode;
  display_name: string;
  description: string;
  permissions: string[];
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
function strings(value: unknown): string[] {
  if (!Array.isArray(value)) throw new Error("Invalid array");
  return value.map(text);
}
function role(value: unknown): RoleCode {
  if (value !== "admin" && value !== "member") throw new Error("Invalid role");
  return value;
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

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly code: string,
    public readonly status: number = 0,
    public readonly details: { field: string; message: string }[] = [],
    public readonly retryAfter: number = 0,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === "VERSION_CONFLICT")
      return "该用户的信息已被其他操作更新，请载入最新信息后再保存。";
    if (error.code === "RATE_LIMITED")
      return `操作过于频繁，请在 ${error.retryAfter || "稍后几"} 秒后重试。`;
    return error.message;
  }
  return "暂时无法完成操作，请重试。";
}
export function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
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

  private async send(
    path: string,
    options: RequestInit = {},
  ): Promise<{ data: unknown; etag: string | null }> {
    this.requireCurrentSession(path);
    const headers = new Headers(options.headers);
    if (this.token) headers.set("Authorization", `Bearer ${this.token}`);
    if (options.body) headers.set("Content-Type", "application/json");
    let response: Response;
    try {
      response = await fetch(`/api/v1${path}`, {
        ...options,
        headers,
        credentials: "omit",
      });
    } catch (error) {
      if (aborted(error)) throw error;
      throw new ApiError("无法连接到服务，请检查连接后重试。", "NETWORK_ERROR");
    }
    this.requireCurrentSession(path);
    if (response.status === 204) return { data: null, etag: null };
    let payload: unknown;
    try {
      payload = await response.json();
    } catch {
      this.requireCurrentSession(path);
      throw new ApiError(
        "服务返回的数据不完整，请重试。",
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
      if (code === "PERMISSION_DENIED" || code === "PASSWORD_CHANGE_REQUIRED")
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
          : "操作失败，请稍后重试。",
        code,
        response.status,
        details,
        Number(response.headers.get("Retry-After")) || 0,
      );
    }
    if (!isObject(payload) || !("data" in payload)) {
      throw new ApiError("服务返回的数据不完整，请重试。", "INVALID_RESPONSE");
    }
    return { data: payload.data, etag: response.headers.get("ETag") };
  }

  private decode<T>(data: unknown, decoder: (value: unknown) => T): T {
    try {
      return decoder(data);
    } catch {
      throw new ApiError(
        "服务返回的数据不完整，请刷新后重试。",
        "INVALID_RESPONSE",
      );
    }
  }
  private record(result: { data: unknown; etag: string | null }): UserRecord {
    const user = this.decode(result.data, decodeUser);
    if (!result.etag || !/^"v[1-9][0-9]*"$/.test(result.etag)) {
      throw new ApiError(
        "无法读取用户的最新版本，请重试。",
        "INVALID_RESPONSE",
      );
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
      if (data.auth_scope !== "full" && data.auth_scope !== "password_change")
        throw new Error("Invalid scope");
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
  async permissions(signal?: AbortSignal): Promise<string[]> {
    const result = await this.send("/users/me/permissions", { signal });
    return this.decode(result.data, (value) =>
      strings(object(value).permissions),
    );
  }
  async roles(signal?: AbortSignal): Promise<RoleDefinition[]> {
    const result = await this.send("/roles", { signal });
    return this.decode(result.data, (value) => {
      if (!Array.isArray(value)) throw new Error("Invalid array");
      return value.map((entry: unknown) => {
        const data = object(entry);
        return {
          code: role(data.code),
          display_name: text(data.display_name),
          description: text(data.description),
          permissions: strings(data.permissions),
        };
      });
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
