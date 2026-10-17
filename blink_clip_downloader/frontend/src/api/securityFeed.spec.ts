import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getSecurityFeedCameras,
  getSecurityFeedSettings,
  getSecurityFeedSnapshot,
  saveSecurityFeedSettings,
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

  describe('getSecurityFeedSnapshot()', () => {
    function blobResponse(blob: Blob, ok = true, status = 200) {
      return { ok, status, blob: () => Promise.resolve(blob) } as Response
    }

    it('resolves the snapshot bytes, asking for it uncached', async () => {
      const blob = new Blob(['jpeg'])
      vi.mocked(fetch).mockResolvedValue(blobResponse(blob))
      const signal = new AbortController().signal

      await expect(getSecurityFeedSnapshot('Front Door', signal)).resolves.toBe(blob)

      expect(fetch).toHaveBeenCalledWith('/api/security-feed/snapshot/Front%20Door', { cache: 'no-store', signal })
    })

    it('URL-encodes special characters in the camera name', async () => {
      vi.mocked(fetch).mockResolvedValue(blobResponse(new Blob(['jpeg'])))

      await getSecurityFeedSnapshot('a/b c')

      expect(vi.mocked(fetch).mock.calls[0][0]).toBe('/api/security-feed/snapshot/a%2Fb%20c')
    })

    it('rejects when the server answers with an error', async () => {
      vi.mocked(fetch).mockResolvedValue(blobResponse(new Blob(['nope']), false, 404))

      await expect(getSecurityFeedSnapshot('Front Door')).rejects.toThrow('Snapshot unavailable: 404')
    })

    it('rejects an empty body rather than handing back an image that cannot be drawn', async () => {
      vi.mocked(fetch).mockResolvedValue(blobResponse(new Blob([])))

      await expect(getSecurityFeedSnapshot('Front Door')).rejects.toThrow('Snapshot was empty')
    })
  })
})
