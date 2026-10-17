import { apiGet, apiPut } from './client'
import { INGRESS_ROOT } from '../env'
import type { SecurityFeedCamerasResponse, SecurityFeedSettings } from './types'

export function getSecurityFeedCameras(): Promise<SecurityFeedCamerasResponse> {
  return apiGet('/api/security-feed/cameras')
}

export function getSecurityFeedSettings(): Promise<SecurityFeedSettings> {
  return apiGet('/api/security-feed/settings')
}

export function saveSecurityFeedSettings(settings: SecurityFeedSettings): Promise<SecurityFeedSettings> {
  return apiPut('/api/security-feed/settings', settings)
}

/** One camera's latest snapshot, fetched whole before anything shows it.
 * Not an <img src>: an <img> whose new src fails to load drops the picture it
 * was already showing, which on a grid that re-requests every tile each tick
 * turned a single failed request into a black, flickering tile. Fetching it
 * here lets the caller keep what it has until a complete image arrives, and
 * an empty body counts as a failure rather than an image that cannot decode.
 * `no-store` mirrors the server's own header: this is live content. */
export async function getSecurityFeedSnapshot(camera: string, signal?: AbortSignal): Promise<Blob> {
  const res = await fetch(`${INGRESS_ROOT}/api/security-feed/snapshot/${encodeURIComponent(camera)}`, {
    cache: 'no-store',
    signal,
  })
  if (!res.ok) throw new Error(`Snapshot unavailable: ${res.status}`)
  const blob = await res.blob()
  if (!blob.size) throw new Error('Snapshot was empty')
  return blob
}
