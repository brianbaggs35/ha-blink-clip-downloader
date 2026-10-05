import { apiGet, apiPost } from './client'
import type { SyncModuleInfo } from './types'

type NetworkId = number | string | null

export function getSyncModules(): Promise<SyncModuleInfo[]> {
  return apiGet('/api/sync-modules')
}

/**
 * The route names the target; *networkId* is what the tab was shown, sent as
 * well so the right module is armed even if it was renamed since (a Mini or
 * doorbell is its own sync module, so renaming the camera renames it).
 */
export function armSyncModule(name: string, armed: boolean, networkId?: NetworkId): Promise<{ armed: boolean }> {
  return apiPost(`/api/sync-modules/${encodeURIComponent(name)}/arm`, {
    armed,
    ...(networkId != null && { network_id: networkId }),
  })
}

/**
 * *cameraId* and *networkId* are what the tab was shown. A name changes when
 * the camera is renamed and two sync modules can hold cameras with the same
 * name; the ids do neither, so they decide which camera is armed.
 */
export function armCamera(
  name: string,
  armed: boolean,
  ids: { cameraId?: string | null; networkId?: NetworkId } = {},
): Promise<{ armed: boolean }> {
  return apiPost(`/api/sync-modules/cameras/${encodeURIComponent(name)}/arm`, {
    armed,
    ...(ids.cameraId != null && { camera_id: ids.cameraId }),
    ...(ids.networkId != null && { network_id: ids.networkId }),
  })
}
