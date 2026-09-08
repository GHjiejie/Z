export function requestIdentity(value: string | null | undefined): string | undefined {
  return value && /^[0-9a-f]{32}$/.test(value) ? value : undefined
}

export function errorWithRequestIdentity(message: string, value: string | null | undefined): string {
  const id = requestIdentity(value)
  return id ? `${message} (Request ID: ${id})` : message
}
