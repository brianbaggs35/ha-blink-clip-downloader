import { apiGet, apiPost } from './client'
import type { ArchiveGroup } from './types'

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

/** Sweeps everything currently eligible for archiving immediately, instead
 * of waiting for the next poll cycle. */
export function runArchiveNow(): Promise<{ archived: number }> {
  return apiPost('/api/storage/archive/run-now')
}
