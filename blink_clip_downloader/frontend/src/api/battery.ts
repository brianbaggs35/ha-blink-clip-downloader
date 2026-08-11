import { apiGet } from './client'
import type { BatteryHistoryEntry, BatteryStatus } from './types'

export function getBatteryStatus(): Promise<BatteryStatus[]> {
  return apiGet('/api/battery/status')
}

export function getBatteryHistory(camera: string): Promise<BatteryHistoryEntry[]> {
  return apiGet(`/api/battery/history/${encodeURIComponent(camera)}`)
}
