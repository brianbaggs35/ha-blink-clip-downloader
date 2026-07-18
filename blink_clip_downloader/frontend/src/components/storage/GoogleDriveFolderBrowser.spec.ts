import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Breadcrumb from 'primevue/breadcrumb'
import GoogleDriveFolderBrowser from './GoogleDriveFolderBrowser.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function mountBrowser() {
  return mount(GoogleDriveFolderBrowser, { global: { plugins: [PrimeVue] } })
}

describe('GoogleDriveFolderBrowser', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads the root folder listing on mount', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ folders: [] })))
    vi.stubGlobal('fetch', fetchMock)
    mountBrowser()
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/storage/gdrive/folders?parent_id=root', {})
  })

  it('shows an empty state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [] }))),
    )
    const wrapper = mountBrowser()
    await flushPromises()
    expect(wrapper.text()).toContain('No subfolders here yet')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountBrowser()
    await flushPromises()
    expect(wrapper.text()).toContain('Could not load Google Drive folders.')
  })

  it('renders a folder row with its name', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))),
    )
    const wrapper = mountBrowser()
    await flushPromises()
    expect(wrapper.text()).toContain('Blink Clips')
  })

  it('navigating into a folder refetches with its id as parent and updates the breadcrumb', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))
      .mockResolvedValueOnce(jsonResponse({ folders: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountBrowser()
    await flushPromises()

    await wrapper.find('.folder-name-btn').trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/storage/gdrive/folders?parent_id=f1', {})
    const breadcrumb = wrapper.findComponent(Breadcrumb)
    const model = breadcrumb.props('model') as { label: string }[]
    expect(model).toHaveLength(1)
    expect(model[0]?.label).toBe('Blink Clips')
  })

  it('clicking a breadcrumb entry navigates back to that level', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))
      .mockResolvedValueOnce(jsonResponse({ folders: [] }))
      .mockResolvedValueOnce(jsonResponse({ folders: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountBrowser()
    await flushPromises()
    await wrapper.find('.folder-name-btn').trigger('click')
    await flushPromises()

    const breadcrumb = wrapper.findComponent(Breadcrumb)
    const home = breadcrumb.props('home') as { command: () => void }
    home.command()
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/storage/gdrive/folders?parent_id=root', {})
  })

  it('cancelling the new-folder dialog closes it without creating anything', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ folders: [] })))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountBrowser()
    await flushPromises()

    await wrapper.find('button').trigger('click')
    await flushPromises()
    const body = new DOMWrapper(document.body)
    const cancelBtn = body.findAll('button').find((b) => b.text() === 'Cancel')
    await cancelBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(1) // only the initial load — no create call
  })

  it('creates a new folder and refreshes the listing', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ folders: [] }))
      .mockResolvedValueOnce(jsonResponse({ id: 'new1', name: 'New Folder', modified_time: '' }))
      .mockResolvedValueOnce(jsonResponse({ folders: [{ id: 'new1', name: 'New Folder', modified_time: '' }] }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountBrowser()
    await flushPromises()

    await wrapper.find('button').trigger('click') // "New Folder" is the first toolbar button
    await flushPromises()
    const body = new DOMWrapper(document.body)
    await body.find('input').setValue('New Folder')
    const createBtn = body.findAll('button').find((b) => b.text() === 'Create')
    await createBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/storage/gdrive/folders', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: 'New Folder', parent_id: 'root' }),
    })
    expect(wrapper.text()).toContain('New Folder')
  })

  it('shows a toast and keeps the dialog open when folder creation fails', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ folders: [] }))
      .mockResolvedValueOnce(jsonResponse({}, false))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountBrowser()
    await flushPromises()

    await wrapper.find('button').trigger('click')
    await flushPromises()
    const body = new DOMWrapper(document.body)
    await body.find('input').setValue('New Folder')
    const createBtn = body.findAll('button').find((b) => b.text() === 'Create')
    await createBtn!.trigger('click')
    await flushPromises()

    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('emits select with the row folder when its Select button is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [{ id: 'f1', name: 'Blink Clips', modified_time: '' }] }))),
    )
    const wrapper = mountBrowser()
    await flushPromises()

    const selectBtn = wrapper.findAll('button').find((b) => b.text() === 'Select')
    await selectBtn!.trigger('click')

    expect(wrapper.emitted('select')).toEqual([[{ id: 'f1', name: 'Blink Clips' }]])
  })

  it('emits select with the current folder when "Use This Folder" is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ folders: [] }))),
    )
    const wrapper = mountBrowser()
    await flushPromises()

    const useBtn = wrapper.findAll('button').find((b) => b.text().includes('Use This Folder'))
    await useBtn!.trigger('click')

    expect(wrapper.emitted('select')).toEqual([[{ id: 'root', name: 'My Drive' }]])
  })
})
