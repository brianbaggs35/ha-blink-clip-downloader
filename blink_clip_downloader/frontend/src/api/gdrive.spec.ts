import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createGDriveFolder,
  disconnectGDrive,
  getGDriveConnectStatus,
  getGDriveQueueStatus,
  getGDriveQuota,
  getGDriveSettings,
  getGDriveStatus,
  listGDriveFolders,
  saveGDriveSettings,
  selectGDriveFolder,
  startGDriveConnect,
  triggerGDriveBackupNow,
  uploadClipsToGDrive,
} from './gdrive'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('gdrive api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getGDriveSettings()', async () => {
    await getGDriveSettings()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/settings', {})
  })

  it('saveGDriveSettings()', async () => {
    await saveGDriveSettings('cid', 'csecret', 'all_clips')
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ client_id: 'cid', client_secret: 'csecret', backup_policy: 'all_clips' }),
    })
  })

  it('saveGDriveSettings() passes null secret through unchanged', async () => {
    await saveGDriveSettings('cid', null, 'archived_only')
    expect(fetch).toHaveBeenCalledWith(
      '/api/storage/gdrive/settings',
      expect.objectContaining({
        body: JSON.stringify({ client_id: 'cid', client_secret: null, backup_policy: 'archived_only' }),
      }),
    )
  })

  it('getGDriveStatus()', async () => {
    await getGDriveStatus()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/status', {})
  })

  it('startGDriveConnect()', async () => {
    await startGDriveConnect()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/connect', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('getGDriveConnectStatus()', async () => {
    await getGDriveConnectStatus()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/connect-status', {})
  })

  it('disconnectGDrive()', async () => {
    await disconnectGDrive()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/disconnect', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('getGDriveQuota()', async () => {
    await getGDriveQuota()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/quota', {})
  })

  it('getGDriveQueueStatus()', async () => {
    await getGDriveQueueStatus()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/queue', {})
  })

  it('listGDriveFolders() defaults to root', async () => {
    await listGDriveFolders()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/folders?parent_id=root', {})
  })

  it('listGDriveFolders() with an explicit parent id', async () => {
    await listGDriveFolders('folder-1')
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/folders?parent_id=folder-1', {})
  })

  it('createGDriveFolder()', async () => {
    await createGDriveFolder('New Folder', 'parent-1')
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'New Folder', parent_id: 'parent-1' }),
    })
  })

  it('selectGDriveFolder()', async () => {
    await selectGDriveFolder('f1', 'Blink Clips')
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/folder', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_id: 'f1', folder_name: 'Blink Clips' }),
    })
  })

  it('triggerGDriveBackupNow()', async () => {
    await triggerGDriveBackupNow()
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/backup-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('uploadClipsToGDrive()', async () => {
    await uploadClipsToGDrive(['c1', 'c2'], 'f1')
    expect(fetch).toHaveBeenCalledWith('/api/storage/gdrive/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_ids: ['c1', 'c2'], folder_id: 'f1' }),
    })
  })

  it('uploadClipsToGDrive() defaults folder_id to empty string', async () => {
    await uploadClipsToGDrive(['c1'])
    expect(fetch).toHaveBeenCalledWith(
      '/api/storage/gdrive/upload',
      expect.objectContaining({ body: JSON.stringify({ clip_ids: ['c1'], folder_id: '' }) }),
    )
  })
})
