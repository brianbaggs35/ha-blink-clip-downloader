import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AiPage from './AiPage.vue'

function jsonResponse(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 500, statusText: 'x', json: () => Promise.resolve(body), text: () => Promise.resolve('') } as Response
}

const AI_STATUS_ENABLED = {
  enabled: true,
  prompt_debug_enabled: false,
  ai_online: true,
  provider: 'anthropic',
  model: 'claude-haiku-4-5',
  smtp_configured: false,
  car_protection_active: null,
  analysis_stats: { total_analyzed: 0, suspicious_count: 0, total_frames_analyzed: 0, frames_analyzed_today: 0, last_analysis: null },
}

function mockFetch(status: unknown = AI_STATUS_ENABLED) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.startsWith('/api/ai/status')) return Promise.resolve(jsonResponse(status))
      if (url.startsWith('/api/ai/camera-configs')) return Promise.resolve(jsonResponse([]))
      if (url.startsWith('/api/ai/faces')) return Promise.resolve(jsonResponse({ available: true, faces: [] }))
      if (url.startsWith('/api/ai/suspicious')) return Promise.resolve(jsonResponse([]))
      if (url.startsWith('/api/ai/feedback/stats')) return Promise.resolve(jsonResponse({ total: 0, correct: 0, incorrect: 0, false_positive: 0, false_negative: 0 }))
      return Promise.reject(new Error(`unexpected fetch ${url}`))
    }),
  )
}

describe('AiPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows a disabled message when AI is not configured', async () => {
    mockFetch({ enabled: false, prompt_debug_enabled: false, smtp_configured: false, analysis_stats: { total_analyzed: 0, suspicious_count: 0, total_frames_analyzed: 0, frames_analyzed_today: 0, last_analysis: null } })
    const wrapper = mount(AiPage)
    await flushPromises()
    expect(wrapper.text()).toContain('AI Analysis Not Configured')
    wrapper.unmount()
  })

  it('renders every card when AI is enabled', async () => {
    mockFetch()
    const wrapper = mount(AiPage)
    await flushPromises()
    expect(wrapper.text()).toContain('AI Connection')
    expect(wrapper.text()).toContain('Schedule')
    expect(wrapper.text()).toContain('Queue Status')
    expect(wrapper.text()).toContain('Analysis Stats')
    expect(wrapper.text()).toContain('Email Alerts')
    expect(wrapper.text()).toContain('Adaptive Learning')
    expect(wrapper.text()).toContain('Camera Configurations')
    expect(wrapper.text()).toContain('Face Recognition Enrollment')
    expect(wrapper.text()).toContain('Suspicious Activity Feed')
    wrapper.unmount()
  })

  it('only shows the Fine-Tuning card for moondream_cloud', async () => {
    mockFetch({ ...AI_STATUS_ENABLED, provider: 'anthropic' })
    const wrapper = mount(AiPage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Fine-Tuning')
    wrapper.unmount()
  })

  it('shows the Fine-Tuning card for moondream_cloud', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/ai/status')) return Promise.resolve(jsonResponse({ ...AI_STATUS_ENABLED, provider: 'moondream_cloud' }))
        if (url.startsWith('/api/ai/finetune')) return Promise.resolve(jsonResponse({ enabled: true, finetunes: [] }))
        if (url.startsWith('/api/ai/feedback/untrained-count')) return Promise.resolve(jsonResponse({ count: 0 }))
        if (url.startsWith('/api/ai/camera-configs')) return Promise.resolve(jsonResponse([]))
        if (url.startsWith('/api/ai/faces')) return Promise.resolve(jsonResponse({ available: true, faces: [] }))
        if (url.startsWith('/api/ai/suspicious')) return Promise.resolve(jsonResponse([]))
        if (url.startsWith('/api/ai/feedback/stats')) return Promise.resolve(jsonResponse({ total: 0, correct: 0, incorrect: 0, false_positive: 0, false_negative: 0 }))
        return Promise.reject(new Error(`unexpected fetch ${url}`))
      }),
    )
    const wrapper = mount(AiPage)
    await flushPromises()
    expect(wrapper.text()).toContain('Fine-Tuning')
    wrapper.unmount()
  })

  it('auto-refreshes status every 10s while mounted', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mount(AiPage)
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.filter((c) => (c[0] as string).startsWith('/api/ai/status')).length
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    const callsAfter = vi.mocked(fetch).mock.calls.filter((c) => (c[0] as string).startsWith('/api/ai/status')).length
    expect(callsAfter).toBeGreaterThan(callsBefore)
    wrapper.unmount()
  })

  it('stops polling after unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    mockFetch()
    const wrapper = mount(AiPage)
    await flushPromises()
    wrapper.unmount()
    const callsAfterUnmount = vi.mocked(fetch).mock.calls.length
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsAfterUnmount)
  })
})
