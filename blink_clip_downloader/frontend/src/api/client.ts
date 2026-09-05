import { INGRESS_ROOT } from '../env'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/** Thin typed fetch wrapper mirroring the pre-Vue UI's `api()` helper. */
async function apiRequest<T>(path: string, opts: RequestInit = {}): Promise<{ data: T; headers: Headers }> {
  const res = await fetch(INGRESS_ROOT + path, opts)
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    throw new ApiError(res.status, `${res.status}: ${text}`)
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
  return api<T>(path, {
    method: 'PUT',
    headers:
      body === undefined && !extraHeaders
        ? undefined
        : { ...(body === undefined ? {} : { 'Content-Type': 'application/json' }), ...extraHeaders },
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

export function apiDelete<T>(path: string): Promise<T> {
  return api<T>(path, { method: 'DELETE' })
}
