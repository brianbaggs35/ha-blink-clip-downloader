import { apiDelete, apiGet, apiPost, apiPut } from './client'
import { INGRESS_ROOT } from '../env'
import type { ActivityRow, CameraStat, ClipDetail, ClipListItem, LibraryStats } from './types'

export interface ClipFilters {
  camera?: string
  since?: string
  until?: string
  starred?: boolean
  source?: string
  tag?: string
  search?: string
  sort?: 'newest' | 'oldest' | 'camera' | 'size' | 'duration'
  limit?: number
  offset?: number
  notified?: boolean
  recognized?: boolean
  archived?: boolean
  archivePath?: string
}

// Sent only when actually set (falsy/empty means "no filter"), verbatim
// string value, same key name as the filter itself.
const STRING_FILTER_FIELDS = ['camera', 'since', 'until', 'source', 'tag', 'search', 'sort'] as const

// Sent only when true (there's no "explicitly false" state for these,
// unlike `starred` below) as the literal string '1'.
const TRUE_ONLY_FLAG_FIELDS = ['notified', 'recognized', 'archived'] as const

function buildQuery(filters: ClipFilters): string {
  const params = new URLSearchParams()
  for (const field of STRING_FILTER_FIELDS) {
    const value = filters[field]
    if (value) params.set(field, value)
  }
  for (const field of TRUE_ONLY_FLAG_FIELDS) {
    if (filters[field]) params.set(field, '1')
  }
  // starred has a meaningful explicit-false state ("only unstarred
  // clips"), so it's checked against undefined rather than truthiness.
  if (filters.starred !== undefined) params.set('starred', filters.starred ? '1' : '0')
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  if (filters.offset !== undefined) params.set('offset', String(filters.offset))
  if (filters.archivePath) params.set('archive_path', filters.archivePath)
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function listClips(filters: ClipFilters = {}): Promise<ClipListItem[]> {
  return apiGet(`/api/clips${buildQuery(filters)}`)
}

export function getClip(id: string): Promise<ClipDetail> {
  return apiGet(`/api/clips/${id}`)
}

export function deleteClip(id: string): Promise<{ deleted: boolean; gdrive_deleted: boolean | null }> {
  return apiDelete(`/api/clips/${id}`)
}

export function starClip(id: string, starred: boolean): Promise<{ id: string; starred: boolean }> {
  return apiPut(`/api/clips/${id}/star`, { starred })
}

export function setClipTags(id: string, tags: string[]): Promise<{ id: string; tags: string[] }> {
  return apiPut(`/api/clips/${id}/tags`, { tags })
}

export function clipStreamUrl(id: string): string {
  return `${INGRESS_ROOT}/api/clips/${id}/stream`
}

export function clipThumbUrl(id: string): string {
  return `${INGRESS_ROOT}/api/clips/${id}/thumb`
}

export function getCameras(): Promise<CameraStat[]> {
  return apiGet('/api/cameras')
}

export function getStats(): Promise<LibraryStats> {
  return apiGet('/api/stats')
}

export function getActivity(days = 7): Promise<ActivityRow[]> {
  return apiGet(`/api/activity?days=${days}`)
}

export function getTags(): Promise<string[]> {
  return apiGet('/api/tags')
}

export async function exportZip(ids: string[]): Promise<Blob> {
  const res = await fetch(`${INGRESS_ROOT}/api/clips/export-zip`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids }),
  })
  if (!res.ok) {
    throw new Error(`Export failed: ${res.status}`)
  }
  return res.blob()
}

export function downloadNow(): Promise<{ triggered: boolean }> {
  return apiPost('/api/download-now')
}
