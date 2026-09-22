import { INGRESS_ROOT } from '../env'

export class ApiError extends Error {
  status: number
  /** The response body as the server sent it — see describeApiError(). */
  body: string
  constructor(status: number, message: string, body = '') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

// Longer than this and a plain-text body is a proxy's error page, not a
// reason worth putting in front of someone.
const MAX_REASON_LENGTH = 200

/**
 * What the server said went wrong — the `error` field of a JSON body, or a
 * short plain-text reason (aiohttp's `HTTPBadRequest(text=...)`) — else
 * *fallback*. For messages a person reads; ApiError.message keeps the raw
 * status and body for logs and tests.
 */
export function describeApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback
  const body = error.body.trim()
  let parsed: unknown
  try {
    parsed = JSON.parse(body)
  } catch {
    // Plain text: a reason, unless it's a proxy's HTML error page or too
    // long to be one.
    return body && !body.startsWith('<') && body.length <= MAX_REASON_LENGTH ? body : fallback
  }
  const reason = (parsed as { error?: unknown } | null)?.error
  return typeof reason === 'string' && reason ? reason : fallback
}

/** Thin typed fetch wrapper mirroring the pre-Vue UI's `api()` helper. */
async function apiRequest<T>(path: string, opts: RequestInit = {}): Promise<{ data: T; headers: Headers }> {
  const res = await fetch(INGRESS_ROOT + path, opts)
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, `${res.status}: ${text}`, text)
  }
  return { data: (await res.json()) as T, headers: res.headers }
}

export async function api<T>(path: string, opts: RequestInit = {}): Promise<T> {
  return (await apiRequest<T>(path, opts)).data
}

export async function apiGetWithHeaders<T>(path: string): Promise<{ data: T; headers: Headers }> {
  return apiRequest<T>(path)
}

export function apiGet<T>(path: string): Promise<T> {
  return api<T>(path)
}

export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'POST',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiPut<T>(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<T> {
  const contentHeaders: Record<string, string> = body === undefined ? {} : { 'Content-Type': 'application/json' }
  let headers: Record<string, string> | undefined
  if (body === undefined && !extraHeaders) {
    headers = undefined
  } else if (extraHeaders) {
    headers = { ...contentHeaders, ...extraHeaders }
  } else {
    headers = contentHeaders
  }

  return api<T>(path, {
    method: 'PUT',
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  return api<T>(path, {
    method: 'PATCH',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export function apiDelete<T>(path: string, body?: unknown): Promise<T> {
  if (body === undefined) return api<T>(path, { method: 'DELETE' })
  return api<T>(path, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
}
