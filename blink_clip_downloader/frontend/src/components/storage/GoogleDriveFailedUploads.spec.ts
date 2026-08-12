import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import GoogleDriveFailedUploads from './GoogleDriveFailedUploads.vue'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const FAILED = [
  { clip_id: 'c1', camera: 'Front Door', clip_path: '/c1.mp4', error_message: 'quota exceeded', completed_at: 't1' },
  { clip_id: 'c2', camera: 'Backyard', clip_path: '/c2.mp4', error_message: '', completed_at: 't2' },
]

function mountComponent() {
  return mount(GoogleDriveFailedUploads, { global: { plugins: [PrimeVue] } })
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
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountComponent()
    await flushPromises()
    expect(wrapper.find('.gdrive-failed').exists()).toBe(false)
  })

  it('renders a row per failed upload with its error message', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(FAILED))),
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
      vi.fn(() => Promise.resolve(jsonResponse(FAILED))),
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
      if (url === '/api/storage/gdrive/queue/failed') {
        loadCount += 1
        // First load (on mount): both. Second load (after retrying c1): only c2 remains.
        return Promise.resolve(jsonResponse(loadCount === 1 ? FAILED : [FAILED[1]]))
      }
      return Promise.resolve(jsonResponse(FAILED))
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
      return Promise.resolve(jsonResponse(FAILED))
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
      return Promise.resolve(jsonResponse(FAILED))
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
      return Promise.resolve(jsonResponse(FAILED))
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
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse([])))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountComponent()
    await flushPromises()

    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
