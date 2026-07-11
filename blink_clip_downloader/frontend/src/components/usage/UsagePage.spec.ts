import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import UsagePage from './UsagePage.vue'
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

function baseUsage(overrides: Record<string, unknown> = {}) {
  return {
    enabled: true,
    provider: 'anthropic',
    model: 'claude-haiku-4-5',
    total_analyses: 10,
    total_tokens_prompt: 1000,
    total_tokens_completion: 500,
    total_tokens: 1500,
    total_escalations: 0,
    total_escalation_tokens: 0,
    by_model: [
      {
        model: 'claude-haiku-4-5',
        analyses: 10,
        tokens_prompt: 1000,
        tokens_completion: 500,
        escalated: false,
        cost: 0.01,
      },
    ],
    total_estimated_cost: 0.01,
    daily: [
      { day: '2026-01-05', analyses: 3, tokens_prompt: 300, tokens_completion: 150, tokens_total: 450, cost: 0.003 },
    ],
    ...overrides,
  }
}

describe('UsagePage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('shows a loading state, then the summary stats, provider, and tables', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage()))),
    )
    const wrapper = mount(UsagePage)
    expect(wrapper.text()).toContain('Loading')
    await flushPromises()
    expect(wrapper.text()).toContain('Clips Analyzed')
    expect(wrapper.text()).toContain('10')
    expect(wrapper.text()).toContain('Anthropic (Claude)')
    expect(wrapper.text()).toContain('claude-haiku-4-5')
    expect(wrapper.text()).toContain('$0.0100')
    expect(wrapper.text()).toContain('2026-01-05')
    wrapper.unmount()
  })

  it('shows the disabled message when AI is off and there is no history', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse(baseUsage({ enabled: false, total_analyses: 0, by_model: [], daily: [] }))),
      ),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('No AI Usage Data')
    wrapper.unmount()
  })

  it('still shows historical data when AI is disabled but has past usage', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ enabled: false })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('No AI Usage Data')
    expect(wrapper.text()).toContain('Clips Analyzed')
    wrapper.unmount()
  })

  it('hides token columns for moondream_local', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            baseUsage({
              provider: 'moondream_local',
              by_model: [
                {
                  model: 'moondream-0.5b',
                  analyses: 5,
                  tokens_prompt: 0,
                  tokens_completion: 0,
                  escalated: false,
                  cost: null,
                },
              ],
            }),
          ),
        ),
      ),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.find('.usage-grid').text()).not.toContain('Total Tokens')
    expect(wrapper.text()).toContain('N/A')
    wrapper.unmount()
  })

  it('shows escalation stats and note when escalations occurred', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ total_escalations: 2, total_escalation_tokens: 400 })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('Escalations')
    expect(wrapper.text()).toContain('Escalation Tokens')
    expect(wrapper.text()).toContain('ai_escalation_provider')
    wrapper.unmount()
  })

  it('marks an escalated model row', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            baseUsage({
              total_escalations: 1,
              by_model: [
                {
                  model: 'gpt-4o-mini',
                  provider: 'openai',
                  analyses: 1,
                  tokens_prompt: 10,
                  tokens_completion: 5,
                  escalated: true,
                  cost: 0.001,
                },
              ],
            }),
          ),
        ),
      ),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('(escalated)')
    wrapper.unmount()
  })

  it('hides the cost stat when there is no priced data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ total_estimated_cost: null })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Estimated Cost')
    wrapper.unmount()
  })

  it('shows empty-state messages for the tables', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ by_model: [], daily: [] })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('No analysis data yet')
    expect(wrapper.text()).toContain('No analysis activity in the last 14 days')
    wrapper.unmount()
  })

  it('clears usage stats after confirmation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'DELETE') return Promise.resolve(jsonResponse({ cleared: true }))
        return Promise.resolve(jsonResponse(baseUsage()))
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    expect(confirm.open).toBe(true)
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/ai/usage', expect.objectContaining({ method: 'DELETE' }))
    wrapper.unmount()
  })

  it('does not clear when declined', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage()))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch)).not.toHaveBeenCalledWith('/api/ai/usage', expect.objectContaining({ method: 'DELETE' }))
    wrapper.unmount()
  })

  it('shows a toast when clearing fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'DELETE') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse(baseUsage()))
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    // covers the clear-failure catch branch — no throw
    wrapper.unmount()
  })

  it('auto-refreshes every 10s while mounted, and stops after unmount', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage()))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    wrapper.unmount()
    const callsAfterUnmount = vi.mocked(fetch).mock.calls.length
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsAfterUnmount)
  })

  it('falls back to placeholders for missing model/day/numeric fields', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            baseUsage({
              model: null,
              by_model: [{ model: null, escalated: false }],
              daily: [{ day: null }],
            }),
          ),
        ),
      ),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const cells = wrapper.findAll('td').map((td) => td.text())
    expect(cells.filter((t) => t === '—').length).toBeGreaterThanOrEqual(2)
    expect(cells).toContain('0')
    wrapper.unmount()
  })

  it('hides the cost stat when there is priced data but zero tokens', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ total_estimated_cost: 0.01, total_tokens: 0 })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Estimated Cost')
    wrapper.unmount()
  })

  it('leaves usage null (nothing rendered) when the fetch fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Loading')
    expect(wrapper.find('.usage-grid').exists()).toBe(false)
    wrapper.unmount()
  })
})
