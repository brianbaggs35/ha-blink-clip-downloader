import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  clearVehicleZone,
  getVehicleSettings,
  saveVehicleSettings,
  saveVehicleZone,
  vehicleZoneSnapshotUrl,
} from './vehicle'
import type { CarZone } from './types'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('vehicle api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getVehicleSettings()', async () => {
    await getVehicleSettings()
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/settings', {})
  })

  it('saveVehicleSettings()', async () => {
    await saveVehicleSettings('Silver Kia Forte')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ car_description: 'Silver Kia Forte' }),
    })
  })

  it('saveVehicleZone()', async () => {
    const zone: CarZone = { shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 }
    await saveVehicleZone('Driveway', zone, 'clip-1')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/zone/Driveway', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zone, clip_id: 'clip-1' }),
    })
  })

  it('saveVehicleZone() URL-encodes the camera name', async () => {
    const zone: CarZone = { shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 }
    await saveVehicleZone('Front Door', zone, 'clip-1')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/zone/Front%20Door', expect.objectContaining({ method: 'PUT' }))
  })

  it('clearVehicleZone()', async () => {
    await clearVehicleZone('Driveway')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/zone/Driveway', { method: 'DELETE' })
  })

  it('vehicleZoneSnapshotUrl()', () => {
    expect(vehicleZoneSnapshotUrl('Driveway')).toBe('/api/vehicle/zone-snapshot/Driveway')
  })
})
