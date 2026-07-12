import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getVehicleSettings, saveVehicleSettings } from './vehicle'

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
})
