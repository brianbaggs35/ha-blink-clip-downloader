import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import StatusPage from './StatusPage.vue'
import { useConnectionStore } from '../../stores/connection'
import { useDateFilterStore } from '../../stores/dateFilter'
import { useRefreshStore } from '../../stores/refresh'

const STATS = {
  connected: true,
  account_id: 'acct-1',
  last_download: '2026-01-05T10:00:00Z',
  total_count: 42,
  today_count: 3,
  week_count: 10,
  starred_count: 5,
  archived_count: 1,
  disk: {
    used_bytes: 900,
    used_mb: 900,
    free_bytes: 1000,
    free_gb: 1,
    total_bytes: 2000,
    total_gb: 2,
    quota_bytes: 1000,
    quota_gb: 1,
  },
}
const CAMERAS = [{ camera: 'front', total: 10, size_bytes: 0, today: 2, this_week: 5, last_seen: '' }]
const ACTIVITY = [{ date: '2026-01-05', hour: 8, count: 4 }]
const AI_STATUS = {
  enabled: true,
  prompt_debug_enabled: false,
  ai_online: true,
  provider: 'anthropic',
  model: 'claude-haiku-4-5',
  smtp_configured: false,
  queue: { pending: 2, processing: 0, completed: 5, failed: 0, in_schedule: true, min_confidence: 0.5, schedule_start: null, schedule_end: null },
  analysis_stats: {
    total_analyzed: 20,
    suspicious_count: 3,
    total_frames_analyzed: 100,
    frames_analyzed_today: 5,
    last_analysis: null,
  },
}

function mockFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.startsWith('/api/stats')) return Promise.resolve({ ok: true, json: () => Promise.resolve(STATS) })
      if (url.startsWith('/api/cameras')) return Promise.resolve({ ok: true, json: () => Promise.resolve(CAMERAS) })
      if (url.startsWith('/api/activity')) return Promise.resolve({ ok: true, json: () => Promise.resolve(ACTIVITY) })
      if (url.startsWith('/api/ai/status')) return Promise.resolve({ ok: true, json: () => Promise.resolve(AI_STATUS) })
      return Promise.reject(new Error(`unexpected fetch ${url}`))
    }),
  )
}

describe('StatusPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockFetch()
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows a loading state, then renders all cards once data resolves', async () => {
    const wrapper = mount(StatusPage)
    expect(wrapper.text()).toContain('Loading')
    await flushPromises()
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.text()).toContain('acct-1')
    expect(wrapper.text()).toContain('42')
    expect(wrapper.text()).toContain('MB')
    expect(wrapper.text()).toContain('front')
    expect(wrapper.text()).toContain('Anthropic (Claude)')
    expect(wrapper.text()).toContain('Online')
  })

  it('reports connectivity to the shared connection store', async () => {
    const wrapper = mount(StatusPage)
    await flushPromises()
    expect(useConnectionStore().connected).toBe(true)
    wrapper.unmount()
  })

  it('requests a date filter when an activity bar is clicked', async () => {
    const wrapper = mount(StatusPage)
    await flushPromises()
    await wrapper.find('.act-bar-wrap').trigger('click')
    expect(useDateFilterStore().date).toBe('2026-01-05')
  })

  it('shows an error state when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve({ ok: false, status: 500, statusText: 'err', text: () => Promise.resolve('') })),
    )
    const wrapper = mount(StatusPage)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load status.')
  })

  it('reloads when the refresh store ticks', async () => {
    const wrapper = mount(StatusPage)
    await flushPromises()
    const calls = vi.mocked(fetch).mock.calls.length
    useRefreshStore().bump()
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(calls)
    wrapper.unmount()
  })
})
