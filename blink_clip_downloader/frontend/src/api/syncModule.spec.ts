import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { armCamera, armSyncModule, getSyncModules } from './syncModule'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('syncModule api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getSyncModules()', async () => {
    await getSyncModules()
    expect(fetch).toHaveBeenCalledWith('/api/sync-modules', {})
  })

  it('armSyncModule()', async () => {
    await armSyncModule('Home', true)
    expect(fetch).toHaveBeenCalledWith('/api/sync-modules/Home/arm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ armed: true }),
    })
  })

  it('armSyncModule() URL-encodes special characters in the name', async () => {
    await armSyncModule('a/b c', false)
    expect(fetch).toHaveBeenCalledWith(
      '/api/sync-modules/a%2Fb%20c/arm',
      expect.objectContaining({ body: JSON.stringify({ armed: false }) }),
    )
  })

  it('armCamera()', async () => {
    await armCamera('Front Door', false)
    expect(fetch).toHaveBeenCalledWith('/api/sync-modules/cameras/Front%20Door/arm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ armed: false }),
    })
  })
})
