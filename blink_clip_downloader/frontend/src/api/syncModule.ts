import { apiGet, apiPost } from './client'
import type { SyncModuleInfo } from './types'

export function getSyncModules(): Promise<SyncModuleInfo[]> {
  return apiGet('/api/sync-modules')
}

export function armSyncModule(name: string, armed: boolean): Promise<{ armed: boolean }> {
  return apiPost(`/api/sync-modules/${encodeURIComponent(name)}/arm`, { armed })
}

export function armCamera(name: string, armed: boolean): Promise<{ armed: boolean }> {
  return apiPost(`/api/sync-modules/cameras/${encodeURIComponent(name)}/arm`, { armed })
}
