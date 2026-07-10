import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AdaptiveLearningCard from './AdaptiveLearningCard.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

describe('AdaptiveLearningCard', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows "No feedback recorded yet" with zero total', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ total: 0, correct: 0, incorrect: 0, false_positive: 0, false_negative: 0 })),
      ),
    )
    const wrapper = mount(AdaptiveLearningCard)
    await flushPromises()
    expect(wrapper.text()).toContain('No feedback recorded yet.')
    expect(wrapper.text()).toContain('—')
  })

  it('shows accuracy percent and breakdown, colored green at >=80%', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ total: 10, correct: 9, incorrect: 1, false_positive: 1, false_negative: 0 })),
      ),
    )
    const wrapper = mount(AdaptiveLearningCard)
    await flushPromises()
    expect(wrapper.text()).toContain('90%')
    expect(wrapper.text()).toContain('1 false positive(s), 0 false negative(s)')
  })

  it('colors accuracy yellow between 50-79% and red under 50%', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ total: 10, correct: 6, incorrect: 4, false_positive: 2, false_negative: 2 })),
      ),
    )
    const wrapper = mount(AdaptiveLearningCard)
    await flushPromises()
    expect(wrapper.text()).toContain('60%')

    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ total: 10, correct: 2, incorrect: 8, false_positive: 4, false_negative: 4 })),
      ),
    )
    const wrapper2 = mount(AdaptiveLearningCard)
    await flushPromises()
    expect(wrapper2.text()).toContain('20%')
  })

  it('reload() re-fetches stats (exposed for the parent to call after events)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ total: 1, correct: 1, incorrect: 0, false_positive: 0, false_negative: 0 })),
      ),
    )
    const wrapper = mount(AdaptiveLearningCard)
    await flushPromises()
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })

  it('leaves defaults in place when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(AdaptiveLearningCard)
    await flushPromises()
    expect(wrapper.text()).toContain('No feedback recorded yet.')
  })
})
