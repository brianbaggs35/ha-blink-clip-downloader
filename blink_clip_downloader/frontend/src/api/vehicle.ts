import { apiDelete, apiGet, apiPut } from './client'
import { INGRESS_ROOT } from '../env'
import type { CarZone, VehicleSettings } from './types'

export function getVehicleSettings(): Promise<VehicleSettings> {
  return apiGet('/api/vehicle/settings')
}

export function saveVehicleSettings(carDescription: string): Promise<{ saved: boolean }> {
  return apiPut('/api/vehicle/settings', { car_description: carDescription })
}

export function saveVehicleZone(
  camera: string,
  zone: CarZone,
  clipId: string,
): Promise<{ saved: boolean; car_zone: CarZone }> {
  return apiPut(`/api/vehicle/zone/${encodeURIComponent(camera)}`, { zone, clip_id: clipId })
}

export function clearVehicleZone(camera: string): Promise<{ saved: boolean }> {
  return apiDelete(`/api/vehicle/zone/${encodeURIComponent(camera)}`)
}

export function vehicleZoneSnapshotUrl(camera: string): string {
  return `${INGRESS_ROOT}/api/vehicle/zone-snapshot/${encodeURIComponent(camera)}`
}
