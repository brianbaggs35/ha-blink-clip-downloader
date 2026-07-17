import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { vi } from 'vitest'
import {
  clipStreamUrl,
  clipThumbUrl,
  deleteClip,
  downloadNow,
  exportZip,
  getActivity,
  getCameras,
  getClip,
  getClipFrames,
  getStats,
  getTags,
  listClips,
  setClipTags,
  starClip,
} from './clips'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: ok ? 'OK' : 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('clips api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('listClips(): builds a query string from filters', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await listClips({
      camera: 'front',
      since: '2026-01-01',
      until: '2026-01-02',
      starred: true,
      source: 'pir',
      tag: 'delivery',
      search: 'box',
      sort: 'newest',
      limit: 48,
      offset: 0,
      notified: true,
    })
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('/api/clips?')
    expect(url).toContain('camera=front')
    expect(url).toContain('starred=1')
    expect(url).toContain('notified=1')
    expect(url).toContain('sort=newest')
  })

  it('listClips(): omits unset filters entirely', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await listClips()
    expect(fetch).toHaveBeenCalledWith('/api/clips', {})
  })

  it('listClips(): starred=false is sent explicitly as 0', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await listClips({ starred: false })
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('starred=0')
  })

  it('getClip() / deleteClip() / starClip() / setClipTags()', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
    await getClip('c1')
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1', {})
    await deleteClip('c1')
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1', { method: 'DELETE' })
    await starClip('c1', true)
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1/star', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ starred: true }),
    })
    await setClipTags('c1', ['a', 'b'])
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1/tags', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tags: ['a', 'b'] }),
    })
  })

  it('clipStreamUrl() / clipThumbUrl() build ingress-relative URLs', () => {
    expect(clipStreamUrl('c1')).toBe('/api/clips/c1/stream')
    expect(clipThumbUrl('c1')).toBe('/api/clips/c1/thumb')
  })

  it('getCameras() / getStats() / getTags()', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await getCameras()
    expect(fetch).toHaveBeenCalledWith('/api/cameras', {})
    await getStats()
    expect(fetch).toHaveBeenCalledWith('/api/stats', {})
    await getTags()
    expect(fetch).toHaveBeenCalledWith('/api/tags', {})
  })

  it('getClipFrames(): omits count by default (server derives it from duration) and forwards a custom count', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ frames: [] }))
    await getClipFrames('c1')
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1/frames', {})
    await getClipFrames('c1', 12)
    expect(fetch).toHaveBeenCalledWith('/api/clips/c1/frames?count=12', {})
  })

  it('getActivity(): defaults to 7 days', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
    await getActivity()
    expect(fetch).toHaveBeenCalledWith('/api/activity?days=7', {})
    await getActivity(30)
    expect(fetch).toHaveBeenCalledWith('/api/activity?days=30', {})
  })

  it('downloadNow(): POSTs with no body', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ triggered: true }))
    await downloadNow()
    expect(fetch).toHaveBeenCalledWith('/api/download-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
  })

  it('exportZip(): POSTs ids and resolves the response blob', async () => {
    const blob = new Blob(['zip'])
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      status: 200,
      blob: () => Promise.resolve(blob),
    } as unknown as Response)
    const result = await exportZip(['a', 'b'])
    expect(result).toBe(blob)
    expect(fetch).toHaveBeenCalledWith('/api/clips/export-zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: ['a', 'b'] }),
    })
  })

  it('exportZip(): throws on failure', async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: false, status: 404 } as Response)
    await expect(exportZip(['a'])).rejects.toThrow('Export failed: 404')
  })
})
