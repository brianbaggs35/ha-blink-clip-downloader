import { apiDelete, apiGet, apiPost, apiPut } from './client'
import { INGRESS_ROOT } from '../env'
import type { AssetActivity, AssetDraft, AssetsResponse, ProtectedAsset } from './types'

export function listAssets(): Promise<AssetsResponse> {
  return apiGet('/api/assets')
}

/** *clipId* is the frame the zone was drawn on; omitted, the camera's saved
 * reference frame is used (only possible once it has one). */
export function createAsset(camera: string, draft: AssetDraft, clipId?: string): Promise<{ asset: ProtectedAsset }> {
  return apiPost('/api/assets', { camera, ...draft, ...(clipId ? { clip_id: clipId } : {}) })
}

export function updateAsset(
  id: string,
  changes: Partial<AssetDraft> & { enabled?: boolean; clip_id?: string },
): Promise<{ asset: ProtectedAsset }> {
  return apiPut(`/api/assets/${encodeURIComponent(id)}`, changes)
}

export function deleteAsset(id: string): Promise<{ deleted: boolean }> {
  return apiDelete(`/api/assets/${encodeURIComponent(id)}`)
}

export function getAssetActivity(days = 7): Promise<{ days: number; activity: AssetActivity[] }> {
  return apiGet(`/api/assets/activity?days=${days}`)
}

/** The frame a camera's assets were drawn on (or its newest clip's). */
export function assetSnapshotUrl(camera: string): string {
  return `${INGRESS_ROOT}/api/assets/snapshot/${encodeURIComponent(camera)}`
}
