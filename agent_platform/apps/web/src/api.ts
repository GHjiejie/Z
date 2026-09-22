let csrfToken = "";
export const API_ROOT = "/api/v1";
export function setCsrf(token: string) {
  csrfToken = token;
}
export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers);
  if (options.body) headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method) && csrfToken)
    headers.set("X-CSRF-Token", csrfToken);
  let response: Response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      ...options,
      headers,
      credentials: "same-origin",
    });
  } catch {
    throw new ApiError(
      "暂时无法连接服务，请检查服务状态后重试。",
      0,
      "network_error",
    );
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    if (
      response.status === 401 &&
      path !== "/auth/login" &&
      path !== "/auth/me"
    )
      window.dispatchEvent(new Event("auth-expired"));
    throw new ApiError(
      body?.error?.message ??
        (response.status === 422
          ? "请检查表单内容是否符合要求。"
          : `请求未完成（${response.status}）`),
      response.status,
      body?.error?.code ?? "request_failed",
    );
  }
  return body as T;
}
export function write<T>(
  path: string,
  body?: unknown,
  method = "POST",
  idempotent = false,
) {
  return api<T>(path, {
    method,
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: idempotent
      ? { "Idempotency-Key": crypto.randomUUID() }
      : undefined,
  });
}
export const money = (value: string | number | null | undefined, digits = 4) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: "USD",
        minimumFractionDigits: 2,
        maximumFractionDigits: digits,
      }).format(Number(value));
export const number = (value: number) =>
  new Intl.NumberFormat("zh-CN").format(value);
export const dateTime = (value: string) =>
  new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(
    new Date(
      value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`,
    ),
  );
export const terminal = (status: string) =>
  [
    "completed",
    "succeeded",
    "failed",
    "cancelled",
    "canceled",
    "interrupted",
    "expired",
  ].includes(status);
