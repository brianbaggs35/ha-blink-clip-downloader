import { api, apiDelete, ApiError, apiGet, apiPost, apiPostForBlob } from './client'
import type { BackupDriveResult, BackupPreview, BackupUpload, RestoreReport, RestoreSummary } from './types'

const BACKUP_URL = '/api/backup'
const DEFAULT_FILENAME = 'blink-clips-backup.zip'

export function getBackupPreview(): Promise<BackupPreview> {
  return apiGet(`${BACKUP_URL}/preview`)
}

/** Makes a backup and returns it as a file, named as the server named it. */
export async function createBackup(includeFaces: boolean): Promise<{ blob: Blob; filename: string }> {
  const { blob, headers } = await apiPostForBlob(`${BACKUP_URL}/create`, { include_faces: includeFaces })
  const named = /filename="([^"]+)"/.exec(headers.get('Content-Disposition') ?? '')
  return { blob, filename: named?.[1] ?? DEFAULT_FILENAME }
}

/** Makes a backup and puts it in the Backups folder in Google Drive. */
export function backupToGDrive(includeFaces: boolean): Promise<BackupDriveResult> {
  return apiPost(`${BACKUP_URL}/gdrive`, { include_faces: includeFaces })
}

// ---------------------------------------------------------------------------
// Restoring: send, look over, do
// ---------------------------------------------------------------------------

/** How many times one piece is tried before the upload gives up, and how
 * long the first retry waits (later ones wait longer). */
const PIECE_ATTEMPTS = 3
const RETRY_DELAY_MS = 1000

type Wait = (ms: number) => Promise<void>
const sleep: Wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms))

function startUpload(size: number): Promise<BackupUpload> {
  return apiPost(`${BACKUP_URL}/uploads`, { size })
}

function sendPiece(uploadId: string, offset: number, piece: Blob, signal?: AbortSignal): Promise<{ received: number }> {
  return api(`${BACKUP_URL}/uploads/${uploadId}?offset=${offset}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/octet-stream' },
    body: piece,
    signal,
  })
}

/** Where the server says it has got to, from a piece it refused as out of
 * order. That happens when a reply was lost and the piece sent again: the
 * server already has it, and the upload carries on from there. */
function resumePoint(error: unknown): number | null {
  if (!(error instanceof ApiError) || error.status !== 409) return null
  try {
    const received = (JSON.parse(error.body) as { received?: unknown } | null)?.received
    return typeof received === 'number' ? received : null
  } catch {
    return null
  }
}

/** A failure that may well not happen again: the connection dropped, or the
 * proxy in front of the add-on had a bad moment. */
function worthRetrying(error: unknown): boolean {
  return error instanceof TypeError || (error instanceof ApiError && error.status >= 500)
}

async function sendWithRetry(
  uploadId: string,
  offset: number,
  piece: Blob,
  signal: AbortSignal | undefined,
  wait: Wait,
): Promise<number> {
  for (let attempt = 1; ; attempt++) {
    try {
      return (await sendPiece(uploadId, offset, piece, signal)).received
    } catch (error) {
      const resumeAt = resumePoint(error)
      if (resumeAt !== null) return resumeAt
      if (signal?.aborted || attempt >= PIECE_ATTEMPTS || !worthRetrying(error)) throw error
      await wait(RETRY_DELAY_MS * attempt)
    }
  }
}

/**
 * Sends *file* to the add-on and returns the id it is held under.
 *
 * In pieces, because Home Assistant's ingress proxy refuses a request body
 * over 16 MiB: a backup sent whole would work on the direct port and fail in
 * the sidebar. Each piece says where it starts, so one repeated after a
 * dropped connection is recognised rather than written twice. `onProgress`
 * hears bytes sent so far.
 */
export async function uploadBackupFile(
  file: File,
  onProgress: (sent: number, total: number) => void,
  signal?: AbortSignal,
  wait: Wait = sleep,
): Promise<string> {
  const { upload_id: uploadId, chunk_bytes: chunkBytes } = await startUpload(file.size)
  let offset = 0
  try {
    while (offset < file.size) {
      if (signal?.aborted) throw new DOMException('The upload was cancelled', 'AbortError')
      const piece = file.slice(offset, Math.min(offset + chunkBytes, file.size))
      offset = await sendWithRetry(uploadId, offset, piece, signal, wait)
      onProgress(Math.min(offset, file.size), file.size)
    }
  } catch (error) {
    // Do not leave a half-sent file taking up space on the add-on.
    await discardBackupUpload(uploadId).catch(() => undefined)
    throw error
  }
  return uploadId
}

/** Checks the uploaded file and says what is in it and what restoring does. */
export function inspectBackup(uploadId: string): Promise<RestoreSummary> {
  return apiPost(`${BACKUP_URL}/uploads/${uploadId}/inspect`)
}

export function restoreBackup(uploadId: string, replaceSettings: boolean): Promise<RestoreReport> {
  return apiPost(`${BACKUP_URL}/uploads/${uploadId}/restore`, { replace_settings: replaceSettings })
}

export function discardBackupUpload(uploadId: string): Promise<{ discarded: boolean }> {
  return apiDelete(`${BACKUP_URL}/uploads/${uploadId}`)
}
