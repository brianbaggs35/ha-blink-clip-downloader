import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Paginator from 'primevue/paginator'
import GoogleDriveFailedUploads from './GoogleDriveFailedUploads.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

/** The failed-uploads endpoint answers with one page plus the overall
 *  total — the page on screen is not the size of the problem when a spell
 *  of Drive being unreachable has failed every clip in the library. */
function page(items: unknown[], total = items.length) {
  return { items, total }
}

const FAILED = [
  { clip_id: 'c1', camera: 'Front Door', clip_path: '/c1.mp4', error_message: 'quota exceeded', completed_at: 't1' },
  { clip_id: 'c2', camera: 'Backyard', clip_path: '/c2.mp4', error_message: '', completed_at: 't2' },
]

function mountComponent() {
  return mount(GoogleDriveFailedUploads)
}

describe('GoogleDriveFailedUploads', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders nothing when there are no failed uploads', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page([])))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.find('.gdrive-failed').exists()).toBe(false)
  })

  it('degrades to the empty state when the response has no page in it', async () => {
    // A render error here would take the whole Storage tab down rather than
    // quietly showing nothing, which is what the defaults are for.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({}))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.find('.gdrive-failed').exists()).toBe(false)
  })

  it('renders a row per failed upload with its error message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page(FAILED)))),
    )
    const wrapper = mountComponent()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed Uploads (2)')
    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('quota exceeded')
  })

  it('falls back to "Unknown error" when no error message was recorded', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page(FAILED)))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.text()).toContain('Unknown error')
  })

  it('treats a fetch failure as no failed uploads rather than crashing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.find('.gdrive-failed').exists()).toBe(false)
  })

  it('retrying one clip calls the retry endpoint with that clip_id, reloads, and emits retried', async () => {
    let loadCount = 0
    const fetchMock = vi.fn((url: string) => {
      if (url === '/api/storage/gdrive/retry') return Promise.resolve(jsonResponse({ retried: 1 }))
      if (url.startsWith('/api/storage/gdrive/queue/failed')) {
        loadCount += 1
        // First load (on mount): both. Second load (after retrying c1): only c2 remains.
        return Promise.resolve(jsonResponse(page(loadCount === 1 ? FAILED : [FAILED[1]])))
      }
      return Promise.resolve(jsonResponse(page(FAILED)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const retryButtons = wrapper.findAll('button').filter((b) => b.text() === 'Retry')
    await retryButtons[0].trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/retry', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_id: 'c1' }),
    })
    expect(wrapper.emitted('retried')).toHaveLength(1)
    // The list reloaded and now only has the second, still-failed clip.
    expect(wrapper.text()).toContain('Backyard')
    expect(wrapper.text()).not.toContain('Front Door')
  })

  it('retrying all calls the retry endpoint with no body', async () => {
    const fetchMock = vi.fn((url: string) => {
      if (url === '/api/storage/gdrive/retry') return Promise.resolve(jsonResponse({ retried: 2 }))
      return Promise.resolve(jsonResponse(page(FAILED)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const retryAllBtn = wrapper.findAll('button').find((b) => b.text() === 'Retry All Failed')
    await retryAllBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/retry', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
    expect(wrapper.emitted('retried')).toHaveLength(1)
  })

  it('shows a toast and does not emit retried when retrying one clip fails', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/gdrive/retry' && opts?.method === 'POST')
        return Promise.resolve({ ok: false, status: 500, statusText: 'err', text: () => Promise.resolve('') })
      return Promise.resolve(jsonResponse(page(FAILED)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const retryButtons = wrapper.findAll('button').filter((b) => b.text() === 'Retry')
    await retryButtons[0].trigger('click')
    await flushPromises()

    expect(wrapper.emitted('retried')).toBeUndefined()
  })

  it('shows a toast and does not emit retried when retrying all fails', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/gdrive/retry' && opts?.method === 'POST')
        return Promise.resolve({ ok: false, status: 500, statusText: 'err', text: () => Promise.resolve('') })
      return Promise.resolve(jsonResponse(page(FAILED)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const retryAllBtn = wrapper.findAll('button').find((b) => b.text() === 'Retry All Failed')
    await retryAllBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('retried')).toBeUndefined()
  })

  it('exposes reload for a parent to call directly', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(page([]))))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('asks the server for one page at a time rather than the whole list', async () => {
    // A spell of Drive being unreachable fails every queued clip at once —
    // this list used to render all of them in one unbounded column.
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(page(FAILED, 40))))
    vi.stubGlobal('fetch', fetchMock)
    mountComponent()
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/queue/failed?limit=10&offset=0', {})
  })

  it('shows the full total, not just what is on the page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page(FAILED, 40)))),
    )
    const wrapper = mountComponent()
    await flushPromises()

    expect(wrapper.text()).toContain('Failed Uploads (40)')
    expect(wrapper.findComponent(Paginator).exists()).toBe(true)
  })

  it('hides the paginator when everything fits on one page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page(FAILED)))),
    )
    const wrapper = mountComponent()
    await flushPromises()

    expect(wrapper.findComponent(Paginator).exists()).toBe(false)
  })

  it('fetches the next page when the paginator moves', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(page(FAILED, 40))))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    wrapper.findComponent(Paginator).vm.$emit('page', { first: 10, rows: 10, page: 1, pageCount: 4 })
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/queue/failed?limit=10&offset=10', {})
  })

  it('summarizes the reasons, so one problem does not read as a page of them', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(page([FAILED[0], { ...FAILED[0], clip_id: 'c3' }, { ...FAILED[0], clip_id: 'c4' }], 3)),
        ),
      ),
    )
    const wrapper = mountComponent()
    await flushPromises()

    expect(wrapper.text()).toContain('quota exceeded — 3 on this page')
  })

  it('dismisses one failure without retrying it', async () => {
    // Retry was the only way to make a failure go away, which is no use at
    // all for one that is never going to succeed.
    let loadCount = 0
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith('/api/storage/gdrive/queue/failed/clear')) {
        return Promise.resolve(jsonResponse({ cleared: 1 }))
      }
      loadCount += 1
      return Promise.resolve(jsonResponse(page(loadCount === 1 ? FAILED : [FAILED[1]])))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const dismiss = wrapper.findAll('button').filter((b) => b.text() === '✕')
    await dismiss[0].trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/queue/failed/clear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_id: 'c1' }),
    })
    expect(wrapper.text()).not.toContain('Front Door')
    expect(wrapper.emitted('retried')).toHaveLength(1)
  })

  it('reports a dismissal that could not be saved', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/storage/gdrive/queue/failed/clear')) return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse(page(FAILED)))
      }),
    )
    const wrapper = mountComponent()
    await flushPromises()

    await wrapper
      .findAll('button')
      .filter((b) => b.text() === '✕')[0]
      .trigger('click')
    await flushPromises()

    expect(useToastStore().message).toBe('Could not clear that failure')
  })

  it('clears every failure after confirming, and says how many went', async () => {
    let cleared = false
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith('/api/storage/gdrive/queue/failed/clear')) {
        cleared = true
        return Promise.resolve(jsonResponse({ cleared: 2 }))
      }
      return Promise.resolve(jsonResponse(page(cleared ? [] : FAILED)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Clear All')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/queue/failed/clear', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    })
    expect(useToastStore().message).toBe('Cleared 2 failed upload(s)')
    expect(wrapper.find('.gdrive-failed').exists()).toBe(false)
  })

  it('does not clear anything when the confirmation is dismissed', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse(page(FAILED))))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Clear All')!
      .trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalledWith('/api/storage/gdrive/queue/failed/clear', expect.anything())
    expect(wrapper.text()).toContain('Front Door')
  })

  it('reports a Clear All that could not be saved', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/storage/gdrive/queue/failed/clear')) return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse(page(FAILED)))
      }),
    )
    const wrapper = mountComponent()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper
      .findAll('button')
      .find((b) => b.text() === 'Clear All')!
      .trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(useToastStore().message).toBe('Could not clear failed uploads')
  })

  it('steps back a page when the last rows on it are cleared', async () => {
    // Otherwise the paginator is stranded on a page that no longer exists,
    // showing an empty list at a non-zero position.
    let total = 11
    const fetchMock = vi.fn((url: string) => {
      if (url.startsWith('/api/storage/gdrive/queue/failed/clear')) {
        total = 10
        return Promise.resolve(jsonResponse({ cleared: 1 }))
      }
      return Promise.resolve(jsonResponse(page(FAILED, total)))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    wrapper.findComponent(Paginator).vm.$emit('page', { first: 10, rows: 10, page: 1, pageCount: 2 })
    await flushPromises()
    await wrapper
      .findAll('button')
      .filter((b) => b.text() === '✕')[0]
      .trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/storage/gdrive/queue/failed?limit=10&offset=0', {})
  })

  it('reloads on a shared refresh tick, so a camera rename is reflected without navigating away', async () => {
    let failed = FAILED
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(page(failed)))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.text()).toContain('Front Door')

    failed = [{ ...FAILED[0], camera: 'Entryway' }, FAILED[1]]
    useRefreshStore().bump()
    await flushPromises()

    expect(wrapper.text()).toContain('Entryway')
    expect(wrapper.text()).not.toContain('Front Door')
  })
})
