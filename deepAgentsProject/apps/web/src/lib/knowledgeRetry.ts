export interface KnowledgeUploadBody {
  filename: string
  content_type: string
  size_bytes: number
  sha256: string
  visibility: 'project'
  allowed_roles: string[]
}

export function uploadHeaders(required: Record<string, string>, size: number) {
  const headers: Record<string, string> = {}
  for (const [name, value] of Object.entries(required)) {
    if (name.toLowerCase() === 'content-length') {
      if (value !== String(size)) throw new Error('File size does not match the upload authorization.')
      // Fetch forbids setting this header. A File body supplies the same length.
    } else headers[name] = value
  }
  return headers
}

async function digest(content: ArrayBuffer) {
  const hash = await crypto.subtle.digest('SHA-256', content)
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export async function knowledgeUploadBody(file: File): Promise<KnowledgeUploadBody> {
  return {
    filename: file.name,
    content_type: file.type || 'application/octet-stream',
    size_bytes: file.size,
    sha256: await digest(await file.arrayBuffer()),
    visibility: 'project',
    allowed_roles: [],
  }
}

/** Retain only opaque keys and request fingerprints, never files or signed URLs.
 * Session storage survives reloads in this tab; clearing it starts a new intent.
 * Storage failures reject before sending a new mutation, avoiding unsafe retries.
 */
export class KnowledgeRetryStore {
  private storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>

  constructor(storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>) {
    this.storage = storage
  }

  async begin(scope: readonly string[], body: unknown) {
    const fingerprint = await digest(new TextEncoder().encode(JSON.stringify([scope, body])).buffer)
    const slot = `deepagent.knowledge.retry.v1.${fingerprint}`
    // Read and write without awaiting between them: concurrent calls in this
    // tab reuse one operation even when their digest promises finish together.
    const key = this.storage.getItem(slot) ?? crypto.randomUUID()
    if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$/.test(key)) {
      throw new Error('Saved knowledge retry state is invalid. Resolve it before submitting again.')
    }
    this.storage.setItem(slot, key)
    return {
      key,
      finish: () => {
        if (this.storage.getItem(slot) === key) this.storage.removeItem(slot)
      },
    }
  }
}
