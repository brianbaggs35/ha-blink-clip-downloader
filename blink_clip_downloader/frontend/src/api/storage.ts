import { apiGet, apiPost } from './client'
import type { ArchiveClipsResponse, ArchiveGroup } from './types'

export interface ArchiveGroupFilters {
  camera?: string
  since?: string
  until?: string
}

function buildQuery(filters: ArchiveGroupFilters): string {
  const params = new URLSearchParams()
  if (filters.camera) params.set('camera', filters.camera)
  if (filters.since) params.set('since', filters.since)
  if (filters.until) params.set('until', filters.until)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function getArchiveGroups(filters: ArchiveGroupFilters = {}): Promise<ArchiveGroup[]> {
  return apiGet(`/api/storage/archives${buildQuery(filters)}`)
}

export interface ArchiveClipFilters {
  archivePath: string
  camera?: string
  since?: string
  until?: string
  limit?: number
  offset?: number
}

export function getArchiveClips(filters: ArchiveClipFilters): Promise<ArchiveClipsResponse> {
  const params = new URLSearchParams({ archive_path: filters.archivePath })
  if (filters.camera) params.set('camera', filters.camera)
  if (filters.since) params.set('since', filters.since)
  if (filters.until) params.set('until', filters.until)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  if (filters.offset !== undefined) params.set('offset', String(filters.offset))
  return apiGet(`/api/storage/archive-clips?${params.toString()}`)
}

/** Sweeps everything currently eligible for archiving immediately, instead
 * of waiting for the next poll cycle. */
export function runArchiveNow(): Promise<{ archived: number }> {
  return apiPost('/api/storage/archive/run-now')
}
