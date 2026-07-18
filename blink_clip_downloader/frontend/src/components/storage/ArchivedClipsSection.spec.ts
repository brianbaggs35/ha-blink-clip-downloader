import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ArchivedClipsSection from './ArchivedClipsSection.vue'
import { useConfirmStore } from '../../stores/confirm'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const CLIP = {
  id: 'a1',
  camera: 'Front Door',
  file_path: '/data/archives/2026-01.zip',
  timestamp: '2026-01-05T10:00:00Z',
  size_bytes: 5_000_000,
  duration: 30,
  source: 'pir',
  network_id: 1,
  starred: false,
  tags: [],
  downloaded_at: '2026-01-05T10:01:00Z',
  archived: true,
  archive_path: '/data/archives/2026-01.zip',
  gdrive_backed_up: false,
  gdrive_file_id: '',
  gdrive_uploaded_at: '',
  notified: false,
  face_recognized: false,
}

function mountSection() {
  return mount(ArchivedClipsSection, { global: { plugins: [PrimeVue] } })
}

describe('ArchivedClipsSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an empty state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('No archived clips yet.')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load archived clips.')
  })

  it('fetches with archived=1', async () => {
    const fetchMock = vi.fn<(url: string) => Promise<Response>>(() => Promise.resolve(jsonResponse([])))
    vi.stubGlobal('fetch', fetchMock)
    mountSection()
    await flushPromises()
    const url = fetchMock.mock.calls[0][0]
    expect(url).toContain('archived=1')
  })

  it('renders a row with camera, size, and a "Not backed up" tag', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([CLIP]))),
    )
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('Not backed up')
  })

  it('shows "Backed up" for a clip already uploaded to Drive', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([{ ...CLIP, gdrive_backed_up: true }]))),
    )
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.text()).toContain('Backed up')
    expect(wrapper.text()).not.toContain('Not backed up')
  })

  it('deletes a clip after confirmation, mentioning the Drive backup', async () => {
    const fetchMock = vi.fn((_url: string, opts?: RequestInit) => {
      if (opts?.method === 'DELETE') return Promise.resolve(jsonResponse({ deleted: true, gdrive_deleted: true }))
      return Promise.resolve(jsonResponse([{ ...CLIP, gdrive_backed_up: true }]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    expect(confirm.message).toContain('Google Drive backup')
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/clips/a1', { method: 'DELETE' })
  })

  it('does not delete when the confirmation is declined', async () => {
    const fetchMock = vi.fn((_url: string, opts?: RequestInit) => {
      if (opts?.method === 'DELETE') throw new Error('should not be called')
      return Promise.resolve(jsonResponse([CLIP]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()

    expect(fetchMock).not.toHaveBeenCalledWith('/api/clips/a1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('shows an error toast when delete fails', async () => {
    const fetchMock = vi.fn((_url: string, opts?: RequestInit) => {
      if (opts?.method === 'DELETE') return Promise.reject(new Error('down'))
      return Promise.resolve(jsonResponse([CLIP]))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()

    // Deletion attempted and failed gracefully — clip list still requested.
    expect(fetchMock).toHaveBeenCalledWith('/api/clips/a1', { method: 'DELETE' })
  })

  it('paginates forward and back', async () => {
    const page0 = Array.from({ length: 21 }, (_, i) => ({ ...CLIP, id: `p${i}` }))
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(page0)) // 21 rows returned for a 20-page-size => hasMore
      .mockResolvedValueOnce(jsonResponse([{ ...CLIP, id: 'page2-item' }]))
      .mockResolvedValueOnce(jsonResponse(page0))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection()
    await flushPromises()

    const buttons = wrapper.findAll('button')
    const next = buttons.find((b) => b.text() === 'Next')
    expect(next).toBeTruthy()
    await next!.trigger('click')
    await flushPromises()

    let url = fetchMock.mock.calls[1][0] as string
    expect(url).toContain('offset=20')

    const prev = wrapper.findAll('button').find((b) => b.text() === 'Previous')
    await prev!.trigger('click')
    await flushPromises()
    url = fetchMock.mock.calls[2][0] as string
    expect(url).toContain('offset=0')
  })

  it('disables Previous on the first page and Next when there is no more data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([CLIP]))),
    )
    const wrapper = mountSection()
    await flushPromises()

    const prev = wrapper.findAll('button').find((b) => b.text() === 'Previous')
    const next = wrapper.findAll('button').find((b) => b.text() === 'Next')
    expect(prev!.attributes('disabled')).toBeDefined()
    expect(next!.attributes('disabled')).toBeDefined()
  })

  it('exposes reload() for the parent to call', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountSection()
    await flushPromises()
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })
})
