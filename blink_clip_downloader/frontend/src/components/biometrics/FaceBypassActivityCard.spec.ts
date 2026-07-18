import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import FaceBypassActivityCard from './FaceBypassActivityCard.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function mockFetch(routes: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      for (const [pattern, body] of Object.entries(routes)) {
        if (url.startsWith(pattern)) {
          if (body instanceof Error) return Promise.reject(body)
          return Promise.resolve(jsonResponse(body))
        }
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`))
    }),
  )
}

function mountCard() {
  return mount(FaceBypassActivityCard, { global: { plugins: [PrimeVue] } })
}

describe('FaceBypassActivityCard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows a not-yet-fired message when total is zero', async () => {
    mockFetch({
      '/api/ai/faces/bypass-stats': { total_bypassed: 0, by_name: [], recent: [] },
      '/api/ai/faces/feedback': [],
    })
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('No suspicious-flag bypass has fired yet')
  })

  it('shows the total, per-name breakdown, and recent activity', async () => {
    mockFetch({
      '/api/ai/faces/bypass-stats': {
        total_bypassed: 3,
        by_name: [
          { name: 'Brian', count: 2 },
          { name: 'Amy', count: 1 },
        ],
        recent: [
          {
            clip_id: 'c3',
            camera: 'Driveway',
            face_bypass_names: 'Brian, Amy',
            analyzed_at: '2026-01-05T10:00:00Z',
          },
          {
            clip_id: 'c2',
            camera: 'Front Door',
            face_bypass_names: 'Brian',
            analyzed_at: '2026-01-04T09:00:00Z',
          },
        ],
      },
      '/api/ai/faces/feedback': [],
    })
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).toContain('Brian × 2')
    expect(wrapper.text()).toContain('Amy × 1')
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Front Door')
  })

  it('shows an error message when the fetch fails', async () => {
    mockFetch({
      '/api/ai/faces/bypass-stats': new Error('down'),
    })
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load bypass activity')
  })

  describe('reported accuracy issues', () => {
    it('shows an empty state when there are no reports', async () => {
      mockFetch({
        '/api/ai/faces/bypass-stats': { total_bypassed: 0, by_name: [], recent: [] },
        '/api/ai/faces/feedback': [],
      })
      const wrapper = mountCard()
      await flushPromises()
      expect(wrapper.text()).toContain('No accuracy reports yet')
    })

    it('lists false-positive and false-negative reports with camera, time, and note', async () => {
      mockFetch({
        '/api/ai/faces/bypass-stats': { total_bypassed: 1, by_name: [{ name: 'Brian', count: 1 }], recent: [] },
        '/api/ai/faces/feedback': [
          {
            clip_id: 'c1',
            camera: 'Front Door',
            report_type: 'false_positive',
            note: 'That was the neighbor',
            person_name: '',
            created_at: '2026-01-06T08:00:00Z',
          },
          {
            clip_id: 'c2',
            camera: 'Driveway',
            report_type: 'false_negative',
            note: '',
            person_name: '',
            created_at: '2026-01-06T09:00:00Z',
          },
        ],
      })
      const wrapper = mountCard()
      await flushPromises()
      expect(wrapper.text()).toContain('Wrong match')
      expect(wrapper.text()).toContain('Missed match')
      expect(wrapper.text()).toContain('Front Door')
      expect(wrapper.text()).toContain('Driveway')
      expect(wrapper.text()).toContain('That was the neighbor')
    })

    it('shows the reported person name when the report identifies one', async () => {
      mockFetch({
        '/api/ai/faces/bypass-stats': { total_bypassed: 0, by_name: [], recent: [] },
        '/api/ai/faces/feedback': [
          {
            clip_id: 'c1',
            camera: 'Driveway',
            report_type: 'false_negative',
            note: '',
            person_name: 'Brian',
            created_at: '2026-01-06T09:00:00Z',
          },
        ],
      })
      const wrapper = mountCard()
      await flushPromises()
      expect(wrapper.text()).toContain('Brian')
    })

    it('still shows the feedback section even when no bypass has fired yet', async () => {
      mockFetch({
        '/api/ai/faces/bypass-stats': { total_bypassed: 0, by_name: [], recent: [] },
        '/api/ai/faces/feedback': [
          {
            clip_id: 'c1',
            camera: 'Front Door',
            report_type: 'false_negative',
            note: '',
            created_at: '2026-01-06T09:00:00Z',
          },
        ],
      })
      const wrapper = mountCard()
      await flushPromises()
      expect(wrapper.text()).toContain('No suspicious-flag bypass has fired yet')
      expect(wrapper.text()).toContain('Missed match')
    })

    it('does not fail the whole card when the feedback fetch fails', async () => {
      mockFetch({
        '/api/ai/faces/bypass-stats': { total_bypassed: 0, by_name: [], recent: [] },
        '/api/ai/faces/feedback': new Error('down'),
      })
      const wrapper = mountCard()
      await flushPromises()
      expect(wrapper.text()).toContain('No suspicious-flag bypass has fired yet')
      expect(wrapper.text()).toContain('No accuracy reports yet')
    })
  })
})
