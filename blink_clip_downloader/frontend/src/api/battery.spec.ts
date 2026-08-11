import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getBatteryHistory, getBatteryStatus } from './battery'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('battery api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getBatteryStatus()', async () => {
    await getBatteryStatus()
    expect(fetch).toHaveBeenCalledWith('/api/battery/status', {})
  })

  it('getBatteryHistory()', async () => {
    await getBatteryHistory('Front Door')
    expect(fetch).toHaveBeenCalledWith('/api/battery/history/Front%20Door', {})
  })

  it('getBatteryHistory() URL-encodes special characters', async () => {
    await getBatteryHistory('a/b c')
    expect(fetch).toHaveBeenCalledWith('/api/battery/history/a%2Fb%20c', {})
  })
})
