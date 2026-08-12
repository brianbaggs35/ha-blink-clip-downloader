import { apiGet, apiPost, apiPut } from './client'
import type {
  GDriveBackupPolicy,
  GDriveConnectState,
  GDriveFailedUpload,
  GDriveFolder,
  GDriveFoldersResponse,
  GDriveQueueStatus,
  GDriveQuota,
  GDriveSettings,
  GDriveStatus,
} from './types'

export function getGDriveSettings(): Promise<GDriveSettings> {
  return apiGet('/api/storage/gdrive/settings')
}

export function saveGDriveSettings(
  clientId: string,
  clientSecret: string | null,
  backupPolicy: GDriveBackupPolicy,
): Promise<{ saved: boolean }> {
  return apiPut('/api/storage/gdrive/settings', {
    client_id: clientId,
    client_secret: clientSecret,
    backup_policy: backupPolicy,
  })
}

export function getGDriveStatus(): Promise<GDriveStatus> {
  return apiGet('/api/storage/gdrive/status')
}

export function startGDriveConnect(): Promise<GDriveConnectState> {
  return apiPost('/api/storage/gdrive/connect')
}

export function getGDriveConnectStatus(): Promise<GDriveConnectState> {
  return apiGet('/api/storage/gdrive/connect-status')
}

export function disconnectGDrive(): Promise<{ disconnected: boolean }> {
  return apiPost('/api/storage/gdrive/disconnect')
}

export function getGDriveQuota(): Promise<GDriveQuota> {
  return apiGet('/api/storage/gdrive/quota')
}

export function getGDriveQueueStatus(): Promise<GDriveQueueStatus> {
  return apiGet('/api/storage/gdrive/queue')
}

export function listGDriveFolders(parentId = 'root'): Promise<GDriveFoldersResponse> {
  return apiGet(`/api/storage/gdrive/folders?parent_id=${encodeURIComponent(parentId)}`)
}

export function createGDriveFolder(name: string, parentId = 'root'): Promise<GDriveFolder> {
  return apiPost('/api/storage/gdrive/folders', { name, parent_id: parentId })
}

export function selectGDriveFolder(folderId: string, folderName: string): Promise<{ saved: boolean }> {
  return apiPut('/api/storage/gdrive/folder', { folder_id: folderId, folder_name: folderName })
}

export function triggerGDriveBackupNow(): Promise<{ enqueued: number }> {
  return apiPost('/api/storage/gdrive/backup-now')
}

export function uploadClipsToGDrive(clipIds: string[], folderId = ''): Promise<{ enqueued: number }> {
  return apiPost('/api/storage/gdrive/upload', { clip_ids: clipIds, folder_id: folderId })
}

export function getFailedGDriveUploads(): Promise<GDriveFailedUpload[]> {
  return apiGet('/api/storage/gdrive/queue/failed')
}

/** Omit clipId to retry every currently-failed upload. */
export function retryFailedGDriveUploads(clipId?: string): Promise<{ retried: number }> {
  return apiPost('/api/storage/gdrive/retry', clipId ? { clip_id: clipId } : undefined)
}
