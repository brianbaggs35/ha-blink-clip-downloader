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
}

function buildQuery(filters: ClipFilters): string {
  const params = new URLSearchParams()
  if (filters.camera) params.set('camera', filters.camera)
  if (filters.since) params.set('since', filters.since)
  if (filters.until) params.set('until', filters.until)
  if (filters.starred !== undefined) params.set('starred', filters.starred ? '1' : '0')
  if (filters.source) params.set('source', filters.source)
  if (filters.tag) params.set('tag', filters.tag)
  if (filters.search) params.set('search', filters.search)
  if (filters.sort) params.set('sort', filters.sort)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  if (filters.offset !== undefined) params.set('offset', String(filters.offset))
  if (filters.notified) params.set('notified', '1')
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export function listClips(filters: ClipFilters = {}): Promise<ClipListItem[]> {
  return apiGet(`/api/clips${buildQuery(filters)}`)
}

export function getClip(id: string): Promise<ClipDetail> {
  return apiGet(`/api/clips/${id}`)
}

export function deleteClip(id: string): Promise<{ deleted: boolean }> {
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

/** ADVANCED FEATURE: several evenly-spaced frames pulled from one clip's
 * video, for the Biometrics tab's "enroll from a clip" flow — lets you pick
 * out whichever frame(s) actually show a face well, since the first frame
 * of a motion-triggered clip often doesn't (see BiometricsPage.vue). */
export function getClipFrames(id: string, count = 8): Promise<{ frames: string[] }> {
  return apiGet(`/api/clips/${id}/frames?count=${count}`)
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
