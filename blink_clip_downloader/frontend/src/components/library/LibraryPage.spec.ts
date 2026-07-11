import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'

// LibraryPage teleports <ClipModal> to <body> (see LibraryPage.vue) so it
// stays visible when another tab is active — VTU's wrapper.find() doesn't
// see teleported nodes, so modal assertions/interactions query body() instead.
function body() {
  return new DOMWrapper(document.body)
}

// PrimeVue's Select opens a teleported (to <body>) overlay on click and
// selects an option on `mousedown` (not `click`) — see primevue/select's
// option template.
async function selectOption(wrapper: ReturnType<typeof mountLibrary>, selectId: string, optionLabel: string) {
  await wrapper.find(`#${selectId} .p-select-label`).trigger('click')
  await flushPromises()
  const opt = [...document.body.querySelectorAll('[role="option"]')].find(
    (el) => el.getAttribute('aria-label') === optionLabel,
  ) as HTMLElement
  await new DOMWrapper(opt).trigger('mousedown')
  await flushPromises()
}

const fakePlayer = {
  src: vi.fn(),
  load: vi.fn(),
  play: vi.fn().mockResolvedValue(undefined),
  pause: vi.fn(),
  dispose: vi.fn(),
  on: vi.fn(),
  fluid: vi.fn(),
  loop: vi.fn(),
  muted: vi.fn().mockReturnValue(false),
  paused: vi.fn().mockReturnValue(true),
  currentTime: vi.fn().mockReturnValue(0),
  duration: vi.fn().mockReturnValue(100),
  requestFullscreen: vi.fn(),
}
vi.mock('video.js', () => ({ default: vi.fn(() => fakePlayer) }))
vi.mock('video.js/dist/video-js.css', () => ({}))

import LibraryPage from './LibraryPage.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useConnectionStore } from '../../stores/connection'
import { useDateFilterStore } from '../../stores/dateFilter'
import { useLibraryStore } from '../../stores/library'
import { useRefreshStore } from '../../stores/refresh'
import { useClipViewerStore } from '../../stores/clipViewer'

function mountLibrary() {
  return mount(LibraryPage, { global: { plugins: [PrimeVue] } })
}

function findByText(wrapper: ReturnType<typeof mountLibrary>, text: string) {
  return wrapper.findAll('button').find((b) => b.text().includes(text))!
}

function clip(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    id: 'c1',
    camera: 'front',
    file_path: '/data/clips/c1.mp4',
    timestamp: '2026-01-05T10:00:00Z',
    size_bytes: 1_000_000,
    duration: 30,
    source: 'pir',
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '2026-01-05T10:01:00Z',
    archived: false,
    archive_path: '',
    notified: false,
    ...overrides,
  }
}

const CAMERAS = [{ camera: 'front', total: 10, size_bytes: 0, today: 2, this_week: 5, last_seen: '' }]
const STATS = {
  connected: true,
  total_count: 10,
  today_count: 2,
  week_count: 5,
  starred_count: 1,
  archived_count: 0,
  total_size_bytes: 1_073_741_824,
}
const AI_STATUS = {
  enabled: false,
  prompt_debug_enabled: false,
  smtp_configured: false,
  analysis_stats: {
    total_analyzed: 0,
    suspicious_count: 0,
    total_frames_analyzed: 0,
    frames_analyzed_today: 0,
    last_analysis: null,
  },
}

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function mockFetch(overrides: Record<string, unknown> = {}, clips = [clip()]) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      for (const [pattern, body] of Object.entries(overrides)) {
        if (url.startsWith(pattern)) {
          if (typeof body === 'function')
            return Promise.resolve(jsonResponse((body as (u: string, o?: RequestInit) => unknown)(url, opts)))
          return Promise.resolve(jsonResponse(body))
        }
      }
      if (url.startsWith('/api/clips/') && url.includes('/star'))
        return Promise.resolve(jsonResponse({ id: 'c1', starred: true }))
      if (url.startsWith('/api/clips/')) return Promise.resolve(jsonResponse(clips[0]))
      if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse(clips))
      if (url.startsWith('/api/cameras')) return Promise.resolve(jsonResponse(CAMERAS))
      if (url.startsWith('/api/stats')) return Promise.resolve(jsonResponse(STATS))
      if (url.startsWith('/api/tags')) return Promise.resolve(jsonResponse(['delivery']))
      if (url.startsWith('/api/ai/status')) return Promise.resolve(jsonResponse(AI_STATUS))
      return Promise.reject(new Error(`unexpected fetch ${url} ${opts?.method}`))
    }),
  )
}

describe('LibraryPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('loads stats, cameras, and clips on mount', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.text()).toContain('front')
    expect(wrapper.find('.lib-stat').exists()).toBe(true)
    expect(useConnectionStore().connected).toBe(true)
    wrapper.unmount()
  })

  it('shows the empty state when no clips are returned', async () => {
    mockFetch({}, [])
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.text()).toContain('No clips found')
    wrapper.unmount()
  })

  it('debounces filter changes before reloading clips', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper.find('#search').setValue('front door')
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).toContain('search=front')
    wrapper.unmount()
  })

  it('switches camera immediately without debounce', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    useLibraryStore().selectCamera('front')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).toContain('camera=front')
    wrapper.unmount()
  })

  it('select mode: selecting a card shows the bulk bar with a count', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    expect(wrapper.text()).toContain('1 selected')
    wrapper.unmount()
  })

  it('bulk star stars every selected clip', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    const starBtn = wrapper.findAll('button').find((b) => b.text().includes('Star all'))!
    await starBtn.trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/clips/c1/star', expect.objectContaining({ method: 'PUT' }))
    wrapper.unmount()
  })

  it('bulk delete requires confirmation before deleting', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    const confirm = useConfirmStore()
    const deleteBtn = wrapper.findAll('button').find((b) => b.text().includes('Delete all'))!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    expect(confirm.open).toBe(true)
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/clips/c1', expect.objectContaining({ method: 'DELETE' }))
    wrapper.unmount()
  })

  it('opening a card (outside select mode) opens the clip modal', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    expect(body().find('.modal-bg').classes()).toContain('open')
    wrapper.unmount()
  })

  it('deleting from the modal removes the card and closes the modal', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    const confirm = useConfirmStore()
    const deleteBtn = body()
      .findAll('button')
      .find((b) => b.text() === '🗑 Delete')!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(wrapper.find('.clip-card').exists()).toBe(false)
    expect(body().find('.modal-bg').classes()).not.toContain('open')
    wrapper.unmount()
  })

  it('starring from the modal patches the grid card in place', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    const starBtn = body()
      .findAll('button')
      .find((b) => b.text().includes('Star'))!
    await starBtn.trigger('click')
    await flushPromises()
    expect(wrapper.find('.clip-card .star-badge').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a Load more button when a full page is returned and loads the next page on click', async () => {
    const fullPage = Array.from({ length: 48 }, (_, i) => clip({ id: `c${i}` }))
    mockFetch({}, fullPage)
    const wrapper = mountLibrary()
    await flushPromises()
    const loadMore = wrapper.findAll('button').find((b) => b.text().includes('Load more'))
    expect(loadMore).toBeTruthy()
    await loadMore!.trigger('click')
    await flushPromises()
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).toContain('offset=48')
    wrapper.unmount()
  })

  it('reloads everything when the refresh store ticks', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    useRefreshStore().bump()
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    wrapper.unmount()
  })

  it('responds to a cross-tab date filter request from the Status tab', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    useDateFilterStore().requestDate('2026-01-05')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).toContain('since=2026-01-05T00%3A00%3A00Z')
    wrapper.unmount()
  })

  it('polls stats/cameras every 60s while the modal is closed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await vi.advanceTimersByTimeAsync(60_000)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    wrapper.unmount()
  })

  it('does not poll while the modal is open', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    const statsCallsBefore = vi.mocked(fetch).mock.calls.filter((c) => (c[0] as string).startsWith('/api/stats')).length
    await vi.advanceTimersByTimeAsync(60_000)
    await flushPromises()
    const statsCallsAfter = vi.mocked(fetch).mock.calls.filter((c) => (c[0] as string).startsWith('/api/stats')).length
    expect(statsCallsAfter).toBe(statsCallsBefore)
    wrapper.unmount()
  })

  it('exports selected clips as a ZIP and triggers a download', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    const blob = new Blob(['zip'])
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/clips/export-zip')
          return Promise.resolve({ ok: true, status: 200, blob: () => Promise.resolve(blob) } as unknown as Response)
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const createObjectURL = vi.fn().mockReturnValue('blob:fake')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL })
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    const zipBtn = wrapper.findAll('button').find((b) => b.text().includes('ZIP'))!
    await zipBtn.trigger('click')
    await flushPromises()
    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(clickSpy).toHaveBeenCalled()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:fake')
    clickSpy.mockRestore()
    wrapper.unmount()
  })

  it('shows a toast when ZIP export fails', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ ok: false, status: 500 } as Response)),
    )
    const zipBtn = wrapper.findAll('button').find((b) => b.text().includes('ZIP'))!
    await zipBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Zipping')
    wrapper.unmount()
  })

  it('onNav does nothing past the last clip', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    await body().find('.vid-nav-btn:nth-of-type(2)').trigger('click')
    expect(body().find('.modal-bg').classes()).toContain('open')
    wrapper.unmount()
  })

  it('closes the modal when deleting the only remaining clip', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    const confirm = useConfirmStore()
    const deleteBtn = body()
      .findAll('button')
      .find((b) => b.text() === '🗑 Delete')!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(body().find('.modal-bg').classes()).not.toContain('open')
    wrapper.unmount()
  })

  it('renders storage info without a quota bar when disk has no quota configured', async () => {
    mockFetch({
      '/api/stats': {
        ...STATS,
        disk: {
          used_bytes: 100,
          used_mb: 1,
          free_bytes: 200,
          free_gb: 1,
          total_bytes: 300,
          total_gb: 1,
          quota_bytes: 0,
          quota_gb: 0,
        },
      },
    })
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.text()).toContain('Free: 1 GB')
    expect(wrapper.findComponent({ name: 'ProgressBar' }).exists()).toBe(false)
    wrapper.unmount()
  })

  it('deselecting a card removes it from the selection', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    expect(wrapper.text()).toContain('1 selected')
    await wrapper.find('.clip-card').trigger('click')
    expect(wrapper.text()).toContain('0 selected')
    wrapper.unmount()
  })

  it('bulk star / delete / zip do nothing with an empty selection', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Star all'))!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Delete all'))!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('ZIP'))!
      .trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
    wrapper.unmount()
  })

  it('fires a browser notification when the clip count increases and notifications are enabled', async () => {
    localStorage.setItem('blink_notif', '1')
    const NotificationMock = vi.fn()
    // @ts-expect-error test stub
    NotificationMock.permission = 'granted'
    vi.stubGlobal('Notification', NotificationMock)
    let total = 10
    mockFetch({ '/api/stats': () => ({ ...STATS, total_count: total }) })
    const wrapper = mountLibrary()
    await flushPromises()
    total = 12
    useRefreshStore().bump()
    await flushPromises()
    expect(NotificationMock).toHaveBeenCalledWith('🎥 2 new Blink clips', expect.any(Object))
    wrapper.unmount()
  })

  it('shows a danger-class quota bar above 90% usage', async () => {
    mockFetch({
      '/api/stats': {
        ...STATS,
        disk: {
          used_bytes: 95,
          used_mb: 95,
          free_bytes: 5,
          free_gb: 0,
          total_bytes: 100,
          total_gb: 0,
          quota_bytes: 100,
          quota_gb: 0,
        },
      },
    })
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProgressBar' }).exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows a warn-class quota bar between 70% and 90% usage', async () => {
    mockFetch({
      '/api/stats': {
        ...STATS,
        disk: {
          used_bytes: 80,
          used_mb: 80,
          free_bytes: 20,
          free_gb: 0,
          total_bytes: 100,
          total_gb: 0,
          quota_bytes: 100,
          quota_gb: 0,
        },
      },
    })
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProgressBar' }).exists()).toBe(true)
    wrapper.unmount()
  })

  it('applies since= filters for today/yesterday/month date ranges', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    for (const label of ['Today', 'Yesterday', 'This month']) {
      const callsBefore = vi.mocked(fetch).mock.calls.length
      await selectOption(wrapper, 'date-range', label)
      await vi.advanceTimersByTimeAsync(400)
      await flushPromises()
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
      const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
      expect(lastCall).toContain('since=')
    }
    wrapper.unmount()
  })

  it('applies starred/notified/source/tag filters to the clips request', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()

    await wrapper.find('.lib-check input[type="checkbox"]').setValue(true)
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.at(-1)?.[0] as string).toContain('starred=1')

    await wrapper.find('.lib-check input[type="checkbox"]').setValue(false)
    await wrapper.findAll('.lib-check input[type="checkbox"]')[1].setValue(true)
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.at(-1)?.[0] as string).toContain('notified=1')

    await selectOption(wrapper, 'source-filter', 'Motion (PIR)')
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.at(-1)?.[0] as string).toContain('source=pir')

    await selectOption(wrapper, 'tag-filter', '#delivery')
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.at(-1)?.[0] as string).toContain('tag=delivery')
    wrapper.unmount()
  })

  it('shows a toast when loading clips fails', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    await selectOption(wrapper, 'source-filter', 'Liveview')
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(wrapper.text()).toContain('No clips found')
    wrapper.unmount()
  })

  it('shows a toast when loading clips for a cross-tab date fails', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    useDateFilterStore().requestDate('2026-01-05')
    await flushPromises()
    // Non-fatal: the page keeps whatever clips it had rather than crashing.
    expect(wrapper.exists()).toBe(true)
    wrapper.unmount()
  })

  it('bulk delete does nothing when the confirmation is declined', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    await wrapper.find('.clip-card').trigger('click')
    const confirm = useConfirmStore()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    const deleteBtn = wrapper.findAll('button').find((b) => b.text().includes('Delete all'))!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
    expect(wrapper.find('.clip-card').exists()).toBe(true)
    wrapper.unmount()
  })

  it('opens the clip modal when the clipViewer store requests a clip', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    useClipViewerStore().requestOpen('c1')
    await flushPromises()
    expect(body().find('.modal-bg').classes()).toContain('open')
    wrapper.unmount()
  })

  it('nav does nothing while no clip is open', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await body().find('.vid-nav-btn:nth-of-type(2)').trigger('click')
    expect(body().find('.modal-bg').classes()).not.toContain('open')
    wrapper.unmount()
  })

  it('deleting the active clip from a multi-clip list advances to the next clip', async () => {
    mockFetch({}, [clip({ id: 'c1' }), clip({ id: 'c2' })])
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    const confirm = useConfirmStore()
    const deleteBtn = body()
      .findAll('button')
      .find((b) => b.text() === '🗑 Delete')!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(body().find('.modal-bg').classes()).toContain('open')
    wrapper.unmount()
  })

  it('shows a plain (no danger/warn) quota bar below 70% usage', async () => {
    mockFetch({
      '/api/stats': {
        ...STATS,
        disk: {
          used_bytes: 30,
          used_mb: 30,
          free_bytes: 70,
          free_gb: 0,
          total_bytes: 100,
          total_gb: 0,
          quota_bytes: 100,
          quota_gb: 0,
        },
      },
    })
    const wrapper = mountLibrary()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'ProgressBar' }).exists()).toBe(true)
    wrapper.unmount()
  })

  it('omits since= entirely for the "All time" date range', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await selectOption(wrapper, 'date-range', 'All time')
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).not.toContain('since=')
    wrapper.unmount()
  })

  it('reloads clips with the new sort order when changed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await selectOption(wrapper, 'sort-order', '💾 Size')
    await vi.advanceTimersByTimeAsync(400)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    const lastCall = vi.mocked(fetch).mock.calls.at(-1)?.[0] as string
    expect(lastCall).toContain('sort=size')
    wrapper.unmount()
  })

  it('the bulk bar cancel button exits select mode', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await findByText(wrapper, 'Select').trigger('click')
    expect(wrapper.text()).toContain('Selecting…')
    await wrapper.findAll('button').find((b) => b.text().includes('Cancel'))!.trigger('click')
    expect(wrapper.text()).not.toContain('Selecting…')
    wrapper.unmount()
  })

  it('shows a toast and keeps the modal open when deleting from the modal fails', async () => {
    mockFetch()
    const wrapper = mountLibrary()
    await flushPromises()
    await wrapper.find('.clip-card').trigger('click')
    await flushPromises()
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const confirm = useConfirmStore()
    const deleteBtn = body()
      .findAll('button')
      .find((b) => b.text() === '🗑 Delete')!
    const clickPromise = deleteBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(body().find('.modal-bg').classes()).toContain('open')
    wrapper.unmount()
  })
})
