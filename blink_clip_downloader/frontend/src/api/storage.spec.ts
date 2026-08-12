import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getArchiveGroups, runArchiveNow } from './storage'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('storage api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse([]))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('getArchiveGroups() with no filters', async () => {
    await getArchiveGroups()
    expect(fetch).toHaveBeenCalledWith('/api/storage/archives', {})
  })

  it('getArchiveGroups() with an empty filters object', async () => {
    await getArchiveGroups({})
    expect(fetch).toHaveBeenCalledWith('/api/storage/archives', {})
  })

  it('getArchiveGroups() with a camera filter', async () => {
    await getArchiveGroups({ camera: 'Front Door' })
    expect(fetch).toHaveBeenCalledWith('/api/storage/archives?camera=Front+Door', {})
  })

  it('getArchiveGroups() with since/until filters', async () => {
    await getArchiveGroups({ since: '2026-06-01T00:00:00+00:00', until: '2026-06-30T23:59:59' })
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('since=2026-06-01T00%3A00%3A00%2B00%3A00')
    expect(url).toContain('until=2026-06-30T23%3A59%3A59')
  })

  it('getArchiveGroups() with all filters combined', async () => {
    await getArchiveGroups({ camera: 'Driveway', since: '2026-06-01', until: '2026-06-30' })
    const url = vi.mocked(fetch).mock.calls[0][0] as string
    expect(url).toContain('camera=Driveway')
    expect(url).toContain('since=2026-06-01')
    expect(url).toContain('until=2026-06-30')
  })

  it('returns the parsed archive group list', async () => {
    const groups = [
      {
        archive_path: '/data/archives/2026-06.zip',
        clip_count: 3,
        total_size: 900,
        latest_timestamp: '2026-06-15T00:00:00+00:00',
      },
    ]
    vi.mocked(fetch).mockResolvedValue(jsonResponse(groups))
    const result = await getArchiveGroups()
    expect(result).toEqual(groups)
  })

  it('runArchiveNow()', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ archived: 5 }))
    const result = await runArchiveNow()
    expect(fetch).toHaveBeenCalledWith('/api/storage/archive/run-now', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
    expect(result).toEqual({ archived: 5 })
  })
})
