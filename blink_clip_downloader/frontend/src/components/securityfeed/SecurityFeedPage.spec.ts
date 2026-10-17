import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import SelectButton from 'primevue/selectbutton'
import SecurityFeedPage from './SecurityFeedPage.vue'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import type { SecurityFeedSettings } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function snapshotResponse(ok = true) {
  return { ok, status: ok ? 200 : 404, blob: () => Promise.resolve(new Blob(['jpeg'])) } as Response
}

const DEFAULT_SETTINGS: SecurityFeedSettings = { cameras: [], columns: 3, refresh_seconds: 15 }

interface Routes {
  cameras?: string[]
  camerasFail?: boolean
  settings?: SecurityFeedSettings
  settingsFail?: boolean
  putResult?: SecurityFeedSettings
  putFail?: boolean
  /** Camera names whose snapshot request is answered with a 404. */
  noSnapshot?: string[]
}

function routedFetch(routes: Routes) {
  return vi.fn((url: string, opts?: RequestInit) => {
    const method = opts?.method
    if (url.startsWith('/api/security-feed/snapshot/')) {
      const camera = decodeURIComponent(url.slice('/api/security-feed/snapshot/'.length))
      return Promise.resolve(snapshotResponse(!routes.noSnapshot?.includes(camera)))
    }
    if (url === '/api/security-feed/cameras') {
      if (routes.camerasFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse({ cameras: routes.cameras ?? [] }))
    }
    if (url === '/api/security-feed/settings' && method === 'PUT') {
      if (routes.putFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse(routes.putResult ?? DEFAULT_SETTINGS))
    }
    if (url === '/api/security-feed/settings') {
      if (routes.settingsFail) return Promise.resolve(jsonResponse({}, false))
      return Promise.resolve(jsonResponse(routes.settings ?? DEFAULT_SETTINGS))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

function mountPage() {
  return mount(SecurityFeedPage)
}

function snapshotRequests(fetchMock: ReturnType<typeof routedFetch>, camera?: string) {
  return fetchMock.mock.calls.filter(
    (c) =>
      c[0].startsWith('/api/security-feed/snapshot/') && (!camera || c[0].endsWith(`/${encodeURIComponent(camera)}`)),
  )
}

describe('SecurityFeedPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    // jsdom implements neither of these.
    let urls = 0
    URL.createObjectURL = vi.fn(() => `blob:snapshot-${++urls}`)
    URL.revokeObjectURL = vi.fn()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('shows a loading indicator while the initial fetch is pending', async () => {
    let resolveFetch: (v: Response) => void = () => {}
    vi.stubGlobal(
      'fetch',
      vi.fn(
        () =>
          new Promise<Response>((resolve) => {
            resolveFetch = resolve
          }),
      ),
    )
    const wrapper = mountPage()
    expect(wrapper.find('.loading-indicator').exists()).toBe(true)
    resolveFetch(jsonResponse({ cameras: [] }))
    await flushPromises()
  })

  it('shows a load error', async () => {
    vi.stubGlobal('fetch', routedFetch({ camerasFail: true }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load the Security Feed')
  })

  it('shows an empty state when no cameras exist', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: [] }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('No cameras found')
  })

  it('shows an info banner explaining tiles only update on new motion', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: ['Front Door'] }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.secfeed-info-banner').text()).toContain('only change when Blink itself records new motion')
  })

  it('renders a tile per camera when no camera filter is saved', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: ['Front Door', 'Backyard'] }))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findAll('.secfeed-tile')).toHaveLength(2)
    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('Backyard')
  })

  it('renders only the saved subset of cameras when one is configured', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        cameras: ['Front Door', 'Backyard', 'Driveway'],
        settings: { cameras: ['Backyard'], columns: 3, refresh_seconds: 15 },
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const tiles = wrapper.findAll('.secfeed-tile')
    expect(tiles).toHaveLength(1)
    expect(wrapper.text()).toContain('Backyard')
    expect(wrapper.text()).not.toContain('Front Door')
  })

  it('ignores a saved camera that no longer exists on the account', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        cameras: ['Front Door'],
        settings: { cameras: ['Removed Camera'], columns: 3, refresh_seconds: 15 },
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    // Falls back to "all cameras" once the saved selection matches nothing
    // that still exists, rather than showing an empty grid.
    expect(wrapper.findAll('.secfeed-tile')).toHaveLength(1)
    expect(wrapper.text()).toContain('Front Door')
  })

  it('applies the configured column count as a CSS variable on the grid', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({ cameras: ['Front Door'], settings: { cameras: [], columns: 3, refresh_seconds: 15 } }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const grid = wrapper.find('.secfeed-grid')
    expect((grid.attributes('style') || '').replace(/\s/g, '')).toContain('--secfeed-columns:3')
  })

  it("shows each camera's snapshot in its own tile", async () => {
    const fetchMock = routedFetch({ cameras: ['Front Door', 'Backyard'] })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    expect(snapshotRequests(fetchMock, 'Front Door')).toHaveLength(1)
    expect(snapshotRequests(fetchMock, 'Backyard')).toHaveLength(1)
    const images = wrapper.findAll('img.secfeed-tile-image')
    expect(images).toHaveLength(2)
    expect(images.map((img) => img.attributes('src'))).toEqual(['blob:snapshot-1', 'blob:snapshot-2'])
    expect(images[0].attributes('alt')).toBe('Front Door snapshot')
  })

  it('shows the placeholder only on the tile whose camera has no snapshot', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: ['Front Door', 'Garage'], noSnapshot: ['Garage'] }))
    const wrapper = mountPage()
    await flushPromises()

    const [front, garage] = wrapper.findAll('.secfeed-tile')
    expect(front.text()).not.toContain('No snapshot available yet')
    expect(front.find('img').classes()).not.toContain('secfeed-tile-image-hidden')
    expect(garage.text()).toContain('No snapshot available yet')
    expect(garage.find('img').classes()).toContain('secfeed-tile-image-hidden')
  })

  it('asks for a fresh snapshot for every tile on every refresh tick', async () => {
    vi.useFakeTimers()
    const fetchMock = routedFetch({
      cameras: ['Front Door', 'Backyard'],
      settings: { cameras: [], columns: 3, refresh_seconds: 15 },
    })
    vi.stubGlobal('fetch', fetchMock)
    mountPage()
    await flushPromises()
    expect(snapshotRequests(fetchMock)).toHaveLength(2)

    await vi.advanceTimersByTimeAsync(15000)
    expect(snapshotRequests(fetchMock)).toHaveLength(4)

    await vi.advanceTimersByTimeAsync(15000)
    expect(snapshotRequests(fetchMock)).toHaveLength(6)
  })

  it('stops refreshing once the page is gone', async () => {
    vi.useFakeTimers()
    const fetchMock = routedFetch({
      cameras: ['Front Door'],
      settings: { cameras: [], columns: 3, refresh_seconds: 15 },
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()
    expect(snapshotRequests(fetchMock)).toHaveLength(1)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(60000)

    expect(snapshotRequests(fetchMock)).toHaveLength(1)
  })

  it('keeps a tile showing its picture when one refresh of one camera fails', async () => {
    vi.useFakeTimers()
    const routes: Routes = {
      cameras: ['Front Door', 'Backyard', 'Garage'],
      settings: { cameras: [], columns: 2, refresh_seconds: 15 },
    }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()
    const third = () => wrapper.findAll('.secfeed-tile')[2]
    const before = third().find('img').attributes('src')
    expect(before).toBeTruthy()

    routes.noSnapshot = ['Garage']
    await vi.advanceTimersByTimeAsync(15000)

    expect(third().find('img').attributes('src')).toBe(before)
    expect(third().find('img').classes()).not.toContain('secfeed-tile-image-hidden')
    expect(third().text()).not.toContain('No snapshot available yet')
  })

  it('populates the customize panel with the loaded settings', async () => {
    vi.stubGlobal(
      'fetch',
      routedFetch({
        cameras: ['Front Door', 'Backyard'],
        settings: { cameras: ['Backyard'], columns: 3, refresh_seconds: 30 },
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.findComponent(MultiSelect).props('modelValue')).toEqual(['Backyard'])
    expect(wrapper.findComponent(SelectButton).props('modelValue')).toBe(3)
    expect(wrapper.findComponent(InputNumber).props('modelValue')).toBe(30)
  })

  it('saves the draft settings and applies them to the grid', async () => {
    const fetchMock = routedFetch({
      cameras: ['Front Door', 'Backyard'],
      settings: { cameras: [], columns: 3, refresh_seconds: 15 },
      putResult: { cameras: ['Backyard'], columns: 2, refresh_seconds: 60 },
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(MultiSelect).vm.$emit('update:modelValue', ['Backyard'])
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 2)
    await wrapper.findComponent(InputNumber).vm.$emit('update:modelValue', 60)
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    const putCall = fetchMock.mock.calls.find(
      (c) => c[0] === '/api/security-feed/settings' && (c[1] as RequestInit)?.method === 'PUT',
    )
    expect(putCall).toBeTruthy()
    expect(JSON.parse((putCall![1] as RequestInit).body as string)).toEqual({
      cameras: ['Backyard'],
      columns: 2,
      refresh_seconds: 60,
    })
    expect(wrapper.findAll('.secfeed-tile')).toHaveLength(1)
    expect(wrapper.text()).toContain('Backyard')
    expect(useToastStore().message).toBe('Security Feed settings saved')
  })

  it('shows an error toast when saving fails', async () => {
    vi.stubGlobal('fetch', routedFetch({ cameras: ['Front Door'], putFail: true }))
    const wrapper = mountPage()
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    expect(useToastStore().message).toBe('Could not save Security Feed settings')
    expect(useToastStore().isError).toBe(true)
  })

  it('restarts the refresh timer at the new interval after saving', async () => {
    vi.useFakeTimers()
    const fetchMock = routedFetch({
      cameras: ['Front Door'],
      settings: { cameras: [], columns: 3, refresh_seconds: 15 },
      putResult: { cameras: [], columns: 3, refresh_seconds: 5 },
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountPage()
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Save')!
      .trigger('click')
    await flushPromises()

    const before = snapshotRequests(fetchMock).length
    await vi.advanceTimersByTimeAsync(5000)
    expect(snapshotRequests(fetchMock)).toHaveLength(before + 1)
    wrapper.unmount()
  })

  it('reloads clean settings on a shared refresh tick (e.g. a camera rename)', async () => {
    const routes: Routes = { cameras: ['Front Door'], settings: { cameras: [], columns: 3, refresh_seconds: 15 } }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    routes.cameras = ['Entryway']
    useRefreshStore().bump()
    await flushPromises()

    expect(wrapper.text()).toContain('Entryway')
  })

  it('keeps the tiles on screen while a shared refresh tick reloads in the background', async () => {
    let releaseReload!: (v: Response) => void
    let settingsCalls = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/security-feed/snapshot/')) return Promise.resolve(snapshotResponse())
        if (url === '/api/security-feed/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
        settingsCalls++
        if (settingsCalls === 2) {
          return new Promise<Response>((resolve) => {
            releaseReload = resolve
          })
        }
        return Promise.resolve(jsonResponse({ cameras: [], columns: 3, refresh_seconds: 15 }))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const tile = wrapper.find('.secfeed-tile').element

    useRefreshStore().bump()
    await flushPromises()

    // The reload is still waiting on the server: the grid must not have been
    // swapped for the loading indicator, which would tear down every tile.
    expect(wrapper.find('.loading-indicator').exists()).toBe(false)
    expect(wrapper.find('.secfeed-tile').element).toBe(tile)

    releaseReload(jsonResponse({ cameras: [], columns: 3, refresh_seconds: 15 }))
    await flushPromises()
    expect(wrapper.find('.secfeed-tile').element).toBe(tile)
  })

  it('does not clobber unsaved draft changes on a shared refresh tick', async () => {
    const routes: Routes = { cameras: ['Front Door'], settings: { cameras: [], columns: 3, refresh_seconds: 15 } }
    vi.stubGlobal('fetch', routedFetch(routes))
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.findComponent(InputNumber).vm.$emit('update:modelValue', 2)

    routes.settings = { cameras: [], columns: 3, refresh_seconds: 30 }
    useRefreshStore().bump()
    await flushPromises()

    expect(wrapper.findComponent(InputNumber).props('modelValue')).toBe(2)
  })

  it('does not let a stale load() response overwrite newer settings', async () => {
    let resolveStale!: (v: Response) => void
    const stale = new Promise<Response>((resolve) => {
      resolveStale = resolve
    })
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/security-feed/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
        if (url === '/api/security-feed/settings') {
          call++
          // Call 1: the initial mount-time load. Call 2: a refresh tick,
          // deferred (this is the one that goes stale). Call 3: a second
          // refresh tick, resolving before call 2 does.
          if (call === 2) return stale
          return Promise.resolve(jsonResponse({ cameras: [], columns: call === 3 ? 1 : 3, refresh_seconds: 15 }))
        }
        return Promise.resolve(jsonResponse({}))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    // Both ticks fire load() while the draft is still clean from the
    // initial load -- call 2 starts (and is deferred), then call 3 starts
    // and resolves immediately, before call 2 ever does.
    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.find('.loading-indicator').exists()).toBe(false)

    resolveStale(jsonResponse({ cameras: [], columns: 3, refresh_seconds: 60 }))
    await flushPromises()

    // Call 3 (columns: 1) must win; call 2's stale response (refresh_seconds:
    // 60) must not overwrite it despite resolving last.
    expect(wrapper.findComponent(InputNumber).props('modelValue')).toBe(15)
  })

  it('does not show a stale load() error after a newer refresh tick already succeeded', async () => {
    let rejectStale!: (err: Error) => void
    const stale = new Promise<Response>((_resolve, reject) => {
      rejectStale = reject
    })
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/security-feed/cameras') return Promise.resolve(jsonResponse({ cameras: ['Front Door'] }))
        if (url === '/api/security-feed/settings') {
          call++
          if (call === 2) return stale
          return Promise.resolve(jsonResponse({ cameras: [], columns: 3, refresh_seconds: 15 }))
        }
        return Promise.resolve(jsonResponse({}))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()

    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    expect(wrapper.text()).not.toContain('Failed to load the Security Feed')

    rejectStale(new Error('network down'))
    await flushPromises()

    expect(wrapper.text()).not.toContain('Failed to load the Security Feed')
  })
})
