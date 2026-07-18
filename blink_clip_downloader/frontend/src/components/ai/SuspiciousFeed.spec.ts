import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Paginator from 'primevue/paginator'
import Select from 'primevue/select'
import SuspiciousFeed from './SuspiciousFeed.vue'
import { useClipViewerStore } from '../../stores/clipViewer'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const ITEM = {
  clip_id: 'c1',
  camera: 'front',
  model: 'claude-haiku-4-5',
  response_text: '',
  is_suspicious: true,
  confidence: 0.85,
  summary: 'Someone lingering at the door',
  frame_count: 4,
  analysis_duration: 1,
  analyzed_at: '2026-01-05T10:00:00Z',
  tokens_prompt: 0,
  tokens_completion: 0,
  anomaly_score: 0,
  escalation_model: '',
  escalation_tokens_prompt: 0,
  escalation_tokens_completion: 0,
  escalation_provider: '',
  prompt_text: '',
  file_path: '',
  clip_timestamp: '',
  duration: 0,
  size_bytes: 0,
}

function mountFeed() {
  return mount(SuspiciousFeed, { global: { plugins: [PrimeVue] } })
}

describe('SuspiciousFeed', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an empty state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [], total: 0 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    expect(wrapper.text()).toContain('No suspicious activity detected yet.')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load suspicious activity.')
  })

  it('renders items with camera, confidence, and summary', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [ITEM], total: 1 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('85%')
    expect(wrapper.text()).toContain('Someone lingering at the door')
  })

  it('requests opening the clip modal when a row is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [ITEM], total: 1 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    await wrapper.find('.card').trigger('click')
    expect(useClipViewerStore().clipId).toBe('c1')
  })

  it('submits quick feedback without opening the modal (stopPropagation)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST')
          return Promise.resolve(jsonResponse({ saved: true }))
        return Promise.resolve(jsonResponse({ items: [ITEM], total: 1 }))
      }),
    )
    const wrapper = mountFeed()
    await flushPromises()
    await wrapper.find('button[title="Correct"]').trigger('click')
    await flushPromises()
    expect(useClipViewerStore().clipId).toBeNull()
    expect(wrapper.text()).toContain('Thanks!')
  })

  it('leaves the feedback buttons in place if submission fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse({ items: [ITEM], total: 1 }))
      }),
    )
    const wrapper = mountFeed()
    await flushPromises()
    await wrapper.find('button[title="Incorrect"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Thanks!')
  })

  it('handles a low-confidence item with no summary/confidence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({ items: [{ ...ITEM, clip_id: 'c2', confidence: undefined, summary: undefined }], total: 1 }),
        ),
      ),
    )
    const wrapper = mountFeed()
    await flushPromises()
    expect(wrapper.text()).toContain('0%')
  })

  it('exposes reload() for the parent to call after events elsewhere', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [], total: 0 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })

  it('defaults to 20 per page, offset 0, and no period filter', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ items: [], total: 0 })))
    vi.stubGlobal('fetch', fetchMock)
    mountFeed()
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/ai/suspicious?limit=20&offset=0', {})
  })

  it('changing the period filter resets to page 1 and refetches with that period', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse({ items: [ITEM], total: 1 })))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountFeed()
    await flushPromises()

    const select = wrapper.findComponent(Select)
    await select.vm.$emit('update:modelValue', 'today')
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/ai/suspicious?limit=20&offset=0&period=today', {})
  })

  it('shows a period-specific empty message once a filter is active', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [ITEM], total: 1 }))
      .mockResolvedValueOnce(jsonResponse({ items: [], total: 0 }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountFeed()
    await flushPromises()

    const select = wrapper.findComponent(Select)
    await select.vm.$emit('update:modelValue', 'yesterday')
    await flushPromises()

    expect(wrapper.text()).toContain('No suspicious activity detected for this period.')
  })

  it('hides the paginator when there are no results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [], total: 0 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    expect(wrapper.findComponent(Paginator).exists()).toBe(false)
  })

  it('shows the paginator with the full total once results exist', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ items: [ITEM], total: 45 }))),
    )
    const wrapper = mountFeed()
    await flushPromises()
    const paginator = wrapper.findComponent(Paginator)
    expect(paginator.exists()).toBe(true)
    expect(paginator.props('totalRecords')).toBe(45)
    expect(paginator.props('rows')).toBe(20)
  })

  it('paging forward requests the next offset/rows and re-renders that page', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ items: [ITEM], total: 45 }))
      .mockResolvedValueOnce(jsonResponse({ items: [{ ...ITEM, clip_id: 'c2' }], total: 45 }))
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountFeed()
    await flushPromises()

    const paginator = wrapper.findComponent(Paginator)
    await paginator.vm.$emit('page', { first: 20, rows: 20, page: 1, pageCount: 3 })
    await flushPromises()

    expect(fetchMock).toHaveBeenLastCalledWith('/api/ai/suspicious?limit=20&offset=20', {})
    expect(useClipViewerStore().clipId).toBeNull()
  })
})
