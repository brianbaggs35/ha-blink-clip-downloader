import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getSecurityFeedCameras,
  getSecurityFeedSettings,
  saveSecurityFeedSettings,
  securityFeedSnapshotUrl,
} from './securityFeed'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('securityFeed api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getSecurityFeedCameras()', async () => {
    await getSecurityFeedCameras()
    expect(fetch).toHaveBeenCalledWith('/api/security-feed/cameras', {})
  })

  it('getSecurityFeedSettings()', async () => {
    await getSecurityFeedSettings()
    expect(fetch).toHaveBeenCalledWith('/api/security-feed/settings', {})
  })

  it('saveSecurityFeedSettings()', async () => {
    const settings = { cameras: ['Front Door'], columns: 3, refresh_seconds: 15 }
    await saveSecurityFeedSettings(settings)
    expect(fetch).toHaveBeenCalledWith('/api/security-feed/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(settings),
    })
  })

  it('securityFeedSnapshotUrl()', () => {
    expect(securityFeedSnapshotUrl('Front Door')).toBe('/api/security-feed/snapshot/Front%20Door')
  })

  it('securityFeedSnapshotUrl() URL-encodes special characters', () => {
    expect(securityFeedSnapshotUrl('a/b c')).toBe('/api/security-feed/snapshot/a%2Fb%20c')
  })
})
