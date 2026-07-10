import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
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
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(SuspiciousFeed)
    await flushPromises()
    expect(wrapper.text()).toContain('No suspicious activity detected yet.')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(SuspiciousFeed)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load suspicious activity.')
  })

  it('renders items with camera, confidence, and summary', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([ITEM]))),
    )
    const wrapper = mount(SuspiciousFeed)
    await flushPromises()
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('85%')
    expect(wrapper.text()).toContain('Someone lingering at the door')
  })

  it('requests opening the clip modal when a row is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([ITEM]))),
    )
    const wrapper = mount(SuspiciousFeed)
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
        return Promise.resolve(jsonResponse([ITEM]))
      }),
    )
    const wrapper = mount(SuspiciousFeed)
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
        return Promise.resolve(jsonResponse([ITEM]))
      }),
    )
    const wrapper = mount(SuspiciousFeed)
    await flushPromises()
    await wrapper.find('button[title="Incorrect"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Thanks!')
  })

  it('exposes reload() for the parent to call after events elsewhere', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(SuspiciousFeed)
    await flushPromises()
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })
})
