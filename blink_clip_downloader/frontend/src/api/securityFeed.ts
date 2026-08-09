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

/** Plain ingress-prefixed URL for an <img src>, not a JSON call — mirrors
 * clipThumbUrl/liveViewPlaylistUrl. Appending a cache-busting timestamp is
 * the caller's job (see SecurityFeedPage.vue's refresh timer) since this
 * function has no notion of "how often" a tile refreshes. */
export function securityFeedSnapshotUrl(camera: string): string {
  return `${INGRESS_ROOT}/api/security-feed/snapshot/${encodeURIComponent(camera)}`
}
