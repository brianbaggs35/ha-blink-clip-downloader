import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import GDriveUploadModal from './GDriveUploadModal.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function mountModal(clipIds = ['c1', 'c2']) {
  return mount(GDriveUploadModal, { props: { clipIds }, global: { plugins: [PrimeVue] } })
}

describe('GDriveUploadModal', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows how many clips will be uploaded', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [] }))),
    )
    mountModal(['c1', 'c2', 'c3'])
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Upload 3 clip(s) to Google Drive')
  })

  it('uploads the given clips to the selected folder and emits uploaded', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/gdrive/upload' && opts?.method === 'POST')
        return Promise.resolve(jsonResponse({ enqueued: 2 }))
      return Promise.resolve(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountModal(['c1', 'c2'])
    await flushPromises()

    const body = new DOMWrapper(document.body)
    const selectBtn = body.findAll('button').find((b) => b.text() === 'Select')
    await selectBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ clip_ids: ['c1', 'c2'], folder_id: 'f1' }),
    })
    expect(wrapper.emitted('uploaded')).toEqual([[2]])
  })

  it('shows an error toast and does not emit uploaded when the request fails', async () => {
    const fetchMock = vi.fn((url: string, opts?: RequestInit) => {
      if (url === '/api/storage/gdrive/upload' && opts?.method === 'POST') return Promise.reject(new Error('down'))
      return Promise.resolve(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))
    })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountModal(['c1'])
    await flushPromises()

    const body = new DOMWrapper(document.body)
    const selectBtn = body.findAll('button').find((b) => b.text() === 'Select')
    await selectBtn!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('uploaded')).toBeUndefined()
  })

  it('emits close when the dialog is dismissed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [] }))),
    )
    const wrapper = mountModal()
    await flushPromises()
    await wrapper.findComponent({ name: 'Dialog' }).vm.$emit('update:visible', false)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
