import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Select from 'primevue/select'
import ClipFaceScanner from './ClipFaceScanner.vue'
import { useRefreshStore } from '../../stores/refresh'
import type { ClipListItem, FaceScanResult } from '../../api/types'
import { candidate, clip, deferred, errorResponse, jsonResponse } from './testing'

const CAMERAS = [
  { camera: 'Front Door', total: 2, size_bytes: 0, today: 0, this_week: 0, last_seen: '' },
  { camera: 'Backyard', total: 1, size_bytes: 0, today: 0, this_week: 0, last_seen: '' },
]

function scanResult(clipId: string, faces = [candidate(`${clipId}-f`)], extra: Partial<FaceScanResult> = {}) {
  return { clip_id: clipId, frames_scanned: 10, duplicates_hidden: 0, faces, ...extra }
}

interface Routes {
  cameras?: unknown
  clips?: (url: string) => ClipListItem[] | Promise<Response>
  scan?: (clipId: string) => Promise<Response>
}

function stubFetch(routes: Routes = {}) {
  const fetchMock = vi.fn((url: string) => {
    if (url.startsWith('/api/cameras')) {
      return routes.cameras instanceof Error
        ? Promise.reject(routes.cameras)
        : Promise.resolve(jsonResponse(routes.cameras ?? CAMERAS))
    }
    if (url.startsWith('/api/clips')) {
      const result = routes.clips?.(url) ?? []
      return Array.isArray(result) ? Promise.resolve(jsonResponse(result)) : result
    }
    const scan = url.match(/^\/api\/ai\/faces\/scan\/(.+)$/)
    if (scan) {
      const id = decodeURIComponent(scan[1])
      return routes.scan?.(id) ?? Promise.resolve(jsonResponse(scanResult(id)))
    }
    return Promise.reject(new Error(`unexpected fetch ${url}`))
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function mountScanner(available = true) {
  return mount(ClipFaceScanner, { props: { available } })
}

function tiles(wrapper: ReturnType<typeof mountScanner>) {
  return wrapper.findAll('.clip-tile')
}

function scanAllButton(wrapper: ReturnType<typeof mountScanner>) {
  return wrapper.find('.scan-all-btn')
}

describe('ClipFaceScanner', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists the clips in range, newest first, with their camera and length', async () => {
    const fetchMock = stubFetch({
      clips: () => [
        clip('old', { timestamp: '2026-01-01T09:00:00Z', camera: 'Backyard' }),
        clip('new', { timestamp: '2026-01-01T10:00:00Z', face_recognized: true }),
      ],
    })
    const wrapper = mountScanner()
    await flushPromises()

    expect(tiles(wrapper)).toHaveLength(2)
    expect(tiles(wrapper)[0].text()).toContain('Front Door')
    expect(tiles(wrapper)[0].text()).toContain('12s')
    expect(tiles(wrapper)[0].text()).toContain('👤')
    expect(tiles(wrapper)[1].text()).toContain('Backyard')
    const clipsUrl = fetchMock.mock.calls.map(([u]) => u).find((u) => u.startsWith('/api/clips'))!
    expect(clipsUrl).toContain('sort=newest')
    expect(clipsUrl).toContain('limit=24')
    expect(clipsUrl).not.toContain('camera=')
    expect(scanAllButton(wrapper).text()).toContain('Find faces in 2 clips')
  })

  it('shows each event once when the USB drive holds a second copy', async () => {
    stubFetch({
      clips: () => [
        clip('cloud', { timestamp: '2026-01-01T10:00:00Z' }),
        clip('local_cloud', { timestamp: '2026-01-01T10:00:01Z', source: 'local_storage' }),
      ],
    })
    const wrapper = mountScanner()
    await flushPromises()
    expect(tiles(wrapper)).toHaveLength(1)
    expect(wrapper.text()).toContain('1 duplicate copy hidden')
  })

  it('says so when there are no clips in range, naming the camera', async () => {
    stubFetch({ clips: () => [] })
    const wrapper = mountScanner()
    await flushPromises()
    expect(wrapper.text()).toContain('No clips in that time range')

    await wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()
    expect(wrapper.text()).toContain('No clips from Backyard in that time range')
  })

  it('labels both pickers by their visible label', async () => {
    stubFetch({ clips: () => [] })
    const wrapper = mountScanner()
    await flushPromises()
    for (const [control, text] of [
      ['biometrics-camera-select', 'Camera'],
      ['biometrics-lookback-select', 'Clips from'],
    ]) {
      // The focusable combobox itself, not PrimeVue's wrapper, carries both.
      const combobox = wrapper.find(`#${control}`)
      expect(combobox.attributes('role')).toBe('combobox')
      const label = wrapper.find(`#${combobox.attributes('aria-labelledby')}`)
      expect(label.text()).toBe(text)
      expect(label.attributes('for')).toBe(control)
    }
  })

  it('filters by camera and time range', async () => {
    const fetchMock = stubFetch({ clips: () => [clip('a')] })
    const wrapper = mountScanner()
    await flushPromises()
    const [cameraSelect, rangeSelect] = wrapper.findAllComponents(Select)
    expect(cameraSelect.props('options')).toEqual([
      { label: 'All cameras', value: '' },
      { label: 'Front Door', value: 'Front Door' },
      { label: 'Backyard', value: 'Backyard' },
    ])

    fetchMock.mockClear()
    await cameraSelect.vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()
    expect(fetchMock.mock.calls[0][0]).toContain('camera=Backyard')
    // A single camera selected: the tile no longer repeats its name.
    expect(tiles(wrapper)[0].find('.clip-tile-camera').exists()).toBe(false)

    const before = Date.now()
    fetchMock.mockClear()
    await rangeSelect.vm.$emit('update:modelValue', 72)
    await flushPromises()
    const since = new URL(`http://x${fetchMock.mock.calls[0][0]}`).searchParams.get('since')!
    expect(before - new Date(since).getTime()).toBeGreaterThanOrEqual(72 * 3_600_000 - 1000)
  })

  it('scans a clip when it is clicked, and says what it found', async () => {
    stubFetch({ clips: () => [clip('a')] })
    const wrapper = mountScanner()
    await flushPromises()

    await tiles(wrapper)[0].trigger('click')
    await flushPromises()

    const found = wrapper.emitted('found')!
    expect(found).toHaveLength(1)
    expect(found[0][0]).toEqual([candidate('a-f')])
    expect((found[0][1] as ClipListItem).id).toBe('a')
    expect(tiles(wrapper)[0].text()).toContain('1 face')
    expect(tiles(wrapper)[0].attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('scanning-change')).toEqual([[true], [false]])
    expect(scanAllButton(wrapper).text()).toContain('All shown clips scanned')
  })

  it('scans clips one at a time, with progress, until stopped', async () => {
    const pending = new Map<string, ReturnType<typeof deferred<Response>>>()
    const fetchMock = stubFetch({
      clips: () => [
        clip('a', { timestamp: '2026-01-01T10:03:00Z' }),
        clip('b', { timestamp: '2026-01-01T10:02:00Z' }),
        clip('c', { timestamp: '2026-01-01T10:01:00Z' }),
      ],
      scan: (id) => {
        const d = deferred<Response>()
        pending.set(id, d)
        return d.promise
      },
    })
    const wrapper = mountScanner()
    await flushPromises()

    await scanAllButton(wrapper).trigger('click')
    await flushPromises()
    expect([...pending.keys()]).toEqual(['a'])
    expect(wrapper.text()).toContain('Scanning clip 1 of 3')
    expect(tiles(wrapper)[0].text()).toContain('Scanning…')
    expect(tiles(wrapper)[1].text()).toContain('Queued')
    // Queued is not scanned: the button must not claim they all are yet.
    expect(scanAllButton(wrapper).text()).toBe('Scanning…')
    expect(scanAllButton(wrapper).attributes('disabled')).toBeDefined()

    pending.get('a')!.resolve(jsonResponse(scanResult('a', [])))
    await flushPromises()
    expect([...pending.keys()]).toEqual(['a', 'b'])
    expect(tiles(wrapper)[0].text()).toContain('No faces')
    expect(wrapper.text()).toContain('Scanning clip 2 of 3')

    const stop = wrapper.findAll('button').find((b) => b.text() === 'Stop')!
    await stop.trigger('click')
    expect(wrapper.text()).toContain('Stopping after this clip')
    pending.get('b')!.resolve(jsonResponse(scanResult('b')))
    await flushPromises()

    // "c" was never scanned, and is offered again rather than left queued.
    expect(fetchMock.mock.calls.filter(([u]) => u.includes('/scan/'))).toHaveLength(2)
    expect(tiles(wrapper)[2].text()).toContain('Scan')
    expect(tiles(wrapper)[2].attributes('disabled')).toBeUndefined()
    expect(scanAllButton(wrapper).text()).toContain('Find faces in 1 clip')
    expect(wrapper.find('.scan-progress').exists()).toBe(false)
  })

  it('adds a clip picked mid-scan to the running queue', async () => {
    const pending = new Map<string, ReturnType<typeof deferred<Response>>>()
    stubFetch({
      clips: () => [clip('a', { timestamp: '2026-01-01T10:03:00Z' }), clip('b', { timestamp: '2026-01-01T10:02:00Z' })],
      scan: (id) => {
        const d = deferred<Response>()
        pending.set(id, d)
        return d.promise
      },
    })
    const wrapper = mountScanner()
    await flushPromises()
    await tiles(wrapper)[0].trigger('click')
    await tiles(wrapper)[1].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Scanning clip 1 of 2')
    pending.get('a')!.resolve(jsonResponse(scanResult('a')))
    await flushPromises()
    pending.get('b')!.resolve(jsonResponse(scanResult('b')))
    await flushPromises()
    expect(wrapper.emitted('found')).toHaveLength(2)
  })

  it('explains each clip the server could not scan', async () => {
    const reasons: Record<string, () => Promise<Response>> = {
      missing: () =>
        Promise.resolve(
          jsonResponse(scanResult('missing', [], { error: "This clip's video file is no longer on disk" })),
        ),
      deleted: () => Promise.resolve(errorResponse(404, 'Clip not found')),
      unavailable: () =>
        Promise.resolve(errorResponse(400, JSON.stringify({ error: 'Face recognition is not available' }))),
      offline: () => Promise.reject(new TypeError('Failed to fetch')),
    }
    stubFetch({
      clips: () => Object.keys(reasons).map((id, i) => clip(id, { timestamp: `2026-01-01T10:0${9 - i}:00Z` })),
      scan: (id) => reasons[id](),
    })
    const wrapper = mountScanner()
    await flushPromises()
    await scanAllButton(wrapper).trigger('click')
    await flushPromises()

    expect(tiles(wrapper).every((t) => t.text().includes('Failed — hover for why'))).toBe(true)
    expect(tiles(wrapper).map((t) => t.attributes('title'))).toEqual([
      "This clip's video file is no longer on disk",
      'This clip is no longer in the library',
      'Face recognition is not available',
      'Scan failed — check your connection and try again',
    ])
    expect(wrapper.emitted('found')).toBeUndefined()
  })

  it('counts faces, duplicates and unknown lengths in plain words', async () => {
    stubFetch({
      clips: () => [
        clip('a', { timestamp: '2026-01-01T10:00:00Z', duration: 0 }),
        clip('a-usb', { timestamp: '2026-01-01T10:00:01Z', source: 'local_storage' }),
        clip('b', { timestamp: '2026-01-01T09:00:00Z' }),
        clip('b-usb', { timestamp: '2026-01-01T09:00:02Z', source: 'local_storage' }),
      ],
      scan: (id) => Promise.resolve(jsonResponse(scanResult(id, [candidate('x'), candidate('y')]))),
    })
    const wrapper = mountScanner()
    await flushPromises()
    expect(wrapper.text()).toContain('2 duplicate copies hidden')
    expect(tiles(wrapper)[0].text()).not.toContain('·')
    await tiles(wrapper)[0].trigger('click')
    await flushPromises()
    expect(tiles(wrapper)[0].text()).toContain('2 faces')
  })

  it('ignores a failed clip list overtaken by a newer request', async () => {
    const slow = deferred<Response>()
    let calls = 0
    stubFetch({
      clips: () => {
        calls++
        return calls === 1
          ? slow.promise
          : Array.from({ length: 24 }, (_, i) =>
              clip(`c${i}`, { timestamp: new Date(Date.UTC(2026, 0, 1) - i * 60_000).toISOString() }),
            )
      },
    })
    const wrapper = mountScanner()
    await flushPromises()
    await wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()
    slow.reject(new TypeError('Failed to fetch'))
    await flushPromises()
    expect(wrapper.findAll('button').some((b) => b.text() === 'Load more clips')).toBe(true)
  })

  it('pages in older clips, never listing one twice', async () => {
    const page = Array.from({ length: 24 }, (_, i) =>
      clip(`c${i}`, { timestamp: new Date(Date.UTC(2026, 0, 1, 10, 0, 0) - i * 60_000).toISOString() }),
    )
    const fetchMock = stubFetch({
      clips: (url) =>
        url.includes('offset=0') ? page : [page[23], clip('older', { timestamp: '2025-12-31T00:00:00Z' })],
    })
    const wrapper = mountScanner()
    await flushPromises()
    const more = wrapper.findAll('button').find((b) => b.text() === 'Load more clips')!
    await more.trigger('click')
    await flushPromises()

    expect(fetchMock.mock.calls.some(([u]) => u.includes('offset=24'))).toBe(true)
    expect(tiles(wrapper)).toHaveLength(25)
    expect(wrapper.findAll('button').some((b) => b.text() === 'Load more clips')).toBe(false)
  })

  it('offers a retry when the clip list cannot load, and keeps the camera list it has', async () => {
    let fail = true
    stubFetch({
      cameras: new Error('down'),
      clips: () => (fail ? Promise.resolve(errorResponse(500)) : [clip('a')]),
    })
    const wrapper = mountScanner()
    await flushPromises()
    expect(wrapper.text()).toContain("Couldn't load clips.")
    expect(wrapper.findAllComponents(Select)[0].props('options')).toEqual([{ label: 'All cameras', value: '' }])

    fail = false
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Try again')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain("Couldn't load clips.")
    expect(tiles(wrapper)).toHaveLength(1)
  })

  it('offers a retry when loading more clips fails, keeping what it has', async () => {
    const page = Array.from({ length: 24 }, (_, i) =>
      clip(`c${i}`, { timestamp: new Date(Date.UTC(2026, 0, 1) - i * 60_000).toISOString() }),
    )
    let failMore = true
    stubFetch({
      clips: (url) =>
        url.includes('offset=0') ? page : failMore ? Promise.resolve(errorResponse(500)) : [clip('older')],
    })
    const wrapper = mountScanner()
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Load more clips')!
      .trigger('click')
    await flushPromises()
    expect(tiles(wrapper)).toHaveLength(24)
    const retry = wrapper.findAll('button').find((b) => b.text() === "Couldn't load more — try again")!
    failMore = false
    await retry.trigger('click')
    await flushPromises()
    expect(tiles(wrapper)).toHaveLength(25)
  })

  it('ignores a clip list that arrives after a newer one was asked for', async () => {
    const slow = deferred<Response>()
    let calls = 0
    stubFetch({
      clips: () => {
        calls++
        return calls === 1 ? slow.promise : [clip('fresh')]
      },
    })
    const wrapper = mountScanner()
    await flushPromises()
    await wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()
    slow.resolve(jsonResponse([clip('stale')]))
    await flushPromises()
    expect(tiles(wrapper).map((t) => t.attributes('aria-label'))).toEqual([expect.stringContaining('Front Door')])
    expect(wrapper.text()).not.toContain('stale')
  })

  it('falls back to all cameras when the chosen one disappears on refresh', async () => {
    const fetchMock = stubFetch({ clips: () => [clip('a')] })
    const wrapper = mountScanner()
    await flushPromises()
    await wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'Backyard')
    await flushPromises()

    fetchMock.mockClear()
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) =>
        Promise.resolve(jsonResponse(url.startsWith('/api/cameras') ? [CAMERAS[0]] : [clip('a')])),
      ),
    )
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.findAllComponents(Select)[0].props('modelValue')).toBe('')
  })

  it('shows a placeholder for a clip without a thumbnail', async () => {
    stubFetch({ clips: () => [clip('a')] })
    const wrapper = mountScanner()
    await flushPromises()
    await wrapper.find('.clip-tile img').trigger('error')
    expect(wrapper.find('.clip-tile-no-thumb').exists()).toBe(true)
  })

  it('offers nothing to scan while face recognition is unavailable', async () => {
    stubFetch({ clips: () => [clip('a')] })
    const wrapper = mountScanner(false)
    await flushPromises()
    expect(scanAllButton(wrapper).attributes('disabled')).toBeDefined()
    expect(tiles(wrapper)[0].attributes('disabled')).toBeDefined()
  })

  it('scans a clip handed to it, only once', async () => {
    const fetchMock = stubFetch({ clips: () => [] })
    const wrapper = mountScanner()
    await flushPromises()
    const vm = wrapper.vm as unknown as { scan: (clips: ClipListItem[]) => void }
    vm.scan([clip('reported')])
    vm.scan([clip('reported')])
    await flushPromises()
    expect(fetchMock.mock.calls.filter(([u]) => u.includes('/scan/reported'))).toHaveLength(1)
    expect(wrapper.emitted('found')).toHaveLength(1)
  })

  it('stops scanning once it is gone', async () => {
    const pending = deferred<Response>()
    const fetchMock = stubFetch({
      clips: () => [clip('a', { timestamp: '2026-01-01T10:01:00Z' }), clip('b', { timestamp: '2026-01-01T10:00:00Z' })],
      scan: () => pending.promise,
    })
    const wrapper = mountScanner()
    await flushPromises()
    await scanAllButton(wrapper).trigger('click')
    await flushPromises()
    wrapper.unmount()
    pending.resolve(jsonResponse(scanResult('a')))
    await flushPromises()
    expect(fetchMock.mock.calls.filter(([u]) => u.includes('/scan/'))).toHaveLength(1)
  })

  it('stops quietly when a scan fails after it is gone', async () => {
    const pending = deferred<Response>()
    stubFetch({ clips: () => [clip('a')], scan: () => pending.promise })
    const wrapper = mountScanner()
    await flushPromises()
    await tiles(wrapper)[0].trigger('click')
    await flushPromises()
    wrapper.unmount()
    pending.reject(new Error('late'))
    await flushPromises()
    expect(wrapper.emitted('found')).toBeUndefined()
  })
})
