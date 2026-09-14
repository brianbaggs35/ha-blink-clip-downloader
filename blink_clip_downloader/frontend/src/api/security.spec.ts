import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getSecurityEvents,
  getSecurityStats,
  getSecurityTimeline,
  getVehicleSignature,
  resetVehicleSignature,
} from './security'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('security api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('requests the timeline with no query when unfiltered', async () => {
    await getSecurityTimeline()
    expect(fetch).toHaveBeenCalledWith('/api/security/timeline', {})
  })

  it('serializes every timeline filter', async () => {
    await getSecurityTimeline({
      limit: 10,
      offset: 20,
      camera: 'Front Door',
      severity: 'suspicious',
      period: 'week',
    })
    expect(fetch).toHaveBeenCalledWith(
      '/api/security/timeline?limit=10&offset=20&camera=Front+Door&severity=suspicious&period=week',
      {},
    )
  })

  it('omits empty filters rather than sending blanks', async () => {
    await getSecurityTimeline({ camera: '', severity: undefined, offset: 0 })
    expect(fetch).toHaveBeenCalledWith('/api/security/timeline?offset=0', {})
  })

  it('requests stats with a default window', async () => {
    await getSecurityStats()
    expect(fetch).toHaveBeenCalledWith('/api/security/stats?days=7', {})
  })

  it('requests stats with an explicit window', async () => {
    await getSecurityStats(30)
    expect(fetch).toHaveBeenCalledWith('/api/security/stats?days=30', {})
  })

  it('escapes the clip id when reading one clip', async () => {
    await getSecurityEvents('clip/1 2')
    expect(fetch).toHaveBeenCalledWith('/api/security/events/clip%2F1%202', {})
  })

  it('reads a learned vehicle signature', async () => {
    await getVehicleSignature('Front Door')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/signature/Front%20Door', {})
  })

  it('resets a learned vehicle signature', async () => {
    await resetVehicleSignature('Front Door')
    expect(fetch).toHaveBeenCalledWith('/api/vehicle/signature/Front%20Door', {
      method: 'DELETE',
    })
  })
})
