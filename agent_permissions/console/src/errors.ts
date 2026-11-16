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
      return "该记录的信息已被其他操作更新，请载入最新信息后再保存";
    if (error.code === "RATE_LIMITED")
      return `操作过于频繁，请在 ${error.retryAfter || "稍后几"} 秒后重试。`;
    return error.message;
  }
  return "暂时无法完成操作，请重试";
}

export function aborted(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
