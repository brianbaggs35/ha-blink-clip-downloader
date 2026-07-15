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

function mountCard() {
  return mount(FaceBypassActivityCard, { global: { plugins: [PrimeVue] } })
}

describe('FaceBypassActivityCard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows a not-yet-fired message when total is zero', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ total_bypassed: 0, by_name: [], recent: [] }))),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('No suspicious-flag bypass has fired yet')
  })

  it('shows the total, per-name breakdown, and recent activity', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({
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
          }),
        ),
      ),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).toContain('Brian × 2')
    expect(wrapper.text()).toContain('Amy × 1')
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Front Door')
  })

  it('shows an error message when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load bypass activity')
  })
})
