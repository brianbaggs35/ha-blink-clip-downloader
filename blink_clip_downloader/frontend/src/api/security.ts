import { apiDelete, apiGet } from './client'
import type { SecurityEventRow, SecurityStats, SecurityTimelineResponse, VehicleSignatureInfo } from './types'

export interface SecurityTimelineParams {
  limit?: number
  offset?: number
  camera?: string
  severity?: string
  period?: string
}

export function getSecurityTimeline(params: SecurityTimelineParams = {}): Promise<SecurityTimelineResponse> {
  const query = new URLSearchParams()
  if (params.limit != null) query.set('limit', String(params.limit))
  if (params.offset != null) query.set('offset', String(params.offset))
  if (params.camera) query.set('camera', params.camera)
  if (params.severity) query.set('severity', params.severity)
  if (params.period) query.set('period', params.period)
  const suffix = query.toString()
  return apiGet(`/api/security/timeline${suffix ? `?${suffix}` : ''}`)
}

export function getSecurityStats(days = 7): Promise<SecurityStats> {
  return apiGet(`/api/security/stats?days=${days}`)
}

export function getSecurityEvents(clipId: string): Promise<{ events: SecurityEventRow[] }> {
  return apiGet(`/api/security/events/${encodeURIComponent(clipId)}`)
}

export function getVehicleSignature(camera: string): Promise<VehicleSignatureInfo> {
  return apiGet(`/api/vehicle/signature/${encodeURIComponent(camera)}`)
}

export function resetVehicleSignature(camera: string): Promise<{ reset: boolean }> {
  return apiDelete(`/api/vehicle/signature/${encodeURIComponent(camera)}`)
}
