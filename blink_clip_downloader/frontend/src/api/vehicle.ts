import { apiGet, apiPut } from './client'
import type { VehicleSettings } from './types'

export function getVehicleSettings(): Promise<VehicleSettings> {
  return apiGet('/api/vehicle/settings')
}

export function saveVehicleSettings(carDescription: string): Promise<{ saved: boolean }> {
  return apiPut('/api/vehicle/settings', { car_description: carDescription })
}
