import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import UsagePage from './UsagePage.vue'
import { useConfirmStore } from '../../stores/confirm'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function basePeriods(overrides: Record<string, unknown> = {}) {
  return {
    weekly: [
      { period: '2026-W03', analyses: 8, tokens_prompt: 800, tokens_completion: 400, tokens_total: 1200, cost: 0.008 },
      { period: '2026-W02', analyses: 2, tokens_prompt: 200, tokens_completion: 100, tokens_total: 300, cost: 0.002 },
    ],
    monthly: [
      { period: '2026-01', analyses: 10, tokens_prompt: 1000, tokens_completion: 500, tokens_total: 1500, cost: 0.01 },
    ],
    ...overrides,
  }
}

/** Stub fetch so the usage poll and the lazily-loaded periods endpoint can
 *  answer independently — the whole point of the periods split is that they
 *  are separate requests. `periods` may be a payload, or null to fail it. */
function stubUsageFetch(usage: unknown, periods: unknown = basePeriods(), periodsOk = true) {
  const calls = { usage: 0, periods: 0 }
  const fetchMock = vi.fn((url: string) => {
    if (String(url).includes('/api/ai/usage/periods')) {
      calls.periods += 1
      return periodsOk
        ? Promise.resolve(jsonResponse(periods))
        : Promise.resolve(jsonResponse({ error: 'nope' }, false))
    }
    calls.usage += 1
    return Promise.resolve(jsonResponse(usage))
  })
  vi.stubGlobal('fetch', fetchMock)
  return calls
}

/** Click one of the Day/Week/Month segmented buttons by its visible label. */
async function selectGranularity(wrapper: ReturnType<typeof mount>, label: string) {
  const option = wrapper
    .findAll('[data-pc-section="pcbutton"], .p-togglebutton, .p-selectbutton button')
    .find((el) => el.text().trim() === label)
  if (!option) throw new Error(`no granularity option labelled ${label}`)
  await option.trigger('click')
  await flushPromises()
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
    // The real backend (_handle_ai_usage) only ever sets "provider" inside
    // its `if enabled:` branch — disabled means that key is entirely absent
    // from the response, not just falsy, so the fixture clears it too
    // rather than keeping baseUsage()'s default 'anthropic'.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ enabled: false, provider: undefined })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('No AI Usage Data')
    expect(wrapper.text()).toContain('Clips Analyzed')
    expect(wrapper.find('.usage-grid').text()).not.toContain('Total Tokens')
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
    expect(wrapper.text()).toContain('AI Escalation Provider')
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

  it('shows the average cost per clip and a distinct models-used count', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage()))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('Avg. Cost / Clip')
    // total_estimated_cost 0.01 / total_analyses 10 = 0.001
    expect(wrapper.text()).toContain('$0.0010')
    expect(wrapper.text()).toContain('Models Used')
    wrapper.unmount()
  })

  it('counts models used by distinct model name, not by row (tier-1 + escalated rows for the same model)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse(
            baseUsage({
              by_model: [
                {
                  model: 'gpt-4o-mini',
                  analyses: 8,
                  tokens_prompt: 800,
                  tokens_completion: 400,
                  escalated: false,
                  cost: 0.005,
                },
                {
                  model: 'gpt-4o-mini',
                  analyses: 2,
                  tokens_prompt: 200,
                  tokens_completion: 100,
                  escalated: true,
                  cost: 0.005,
                },
              ],
            }),
          ),
        ),
      ),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    const modelsUsedTile = wrapper.findAll('.usage-stat').find((t) => t.text().includes('Models Used'))!
    expect(modelsUsedTile.text()).toContain('1')
    wrapper.unmount()
  })

  it('hides the average-cost-per-clip stat when there is no priced data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ total_estimated_cost: null })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Avg. Cost / Clip')
    wrapper.unmount()
  })

  it('hides the average-cost-per-clip stat when there have been zero analyses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ total_analyses: 0, total_tokens: 100 })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Avg. Cost / Clip')
    wrapper.unmount()
  })

  it('hides the models-used stat when there is no per-model data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ by_model: [] })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Models Used')
    wrapper.unmount()
  })

  it('tolerates a malformed response with by_model missing entirely', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ by_model: undefined })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Models Used')
    expect(wrapper.text()).toContain('No analysis data yet')
    wrapper.unmount()
  })

  it('tolerates a malformed response with daily missing entirely', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(baseUsage({ daily: undefined })))),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).toContain('No analysis activity in the last 14 days')
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

  it('ignores a poll response that lands after Clear Stats emptied the stats', async () => {
    // The 10s poll and Clear Stats' own reload are two concurrent loads: a
    // poll issued just before the clear used to resolve after it and paint
    // the pre-clear numbers straight back over the cleared ones.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let releaseStalePoll: (() => void) | undefined
    let gets = 0
    const cleared = baseUsage({
      total_analyses: 0,
      total_tokens: 0,
      total_estimated_cost: 0,
      by_model: [],
      daily: [],
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'DELETE') return Promise.resolve(jsonResponse({ cleared: true }))
        gets += 1
        if (gets === 2) {
          return new Promise<Response>((resolve) => {
            releaseStalePoll = () => resolve(jsonResponse(baseUsage()))
          })
        }
        return Promise.resolve(jsonResponse(gets === 1 ? baseUsage() : cleared))
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    expect(wrapper.text()).not.toContain('No analysis data yet')

    vi.advanceTimersByTime(10_000)
    await flushPromises()
    expect(releaseStalePoll).toBeDefined()

    const confirm = useConfirmStore()
    const clickPromise = wrapper.find('button').trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(wrapper.text()).toContain('No analysis data yet')

    releaseStalePoll?.()
    await flushPromises()
    expect(wrapper.text()).toContain('No analysis data yet')
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
    const toast = useToastStore()
    expect(toast.message).toBe('Failed to clear usage stats')
    expect(toast.isError).toBe(true)
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
    expect(vi.mocked(fetch).mock.calls).toHaveLength(callsAfterUnmount)
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

  // ── Weekly / monthly usage history ──────────────────────────────

  it('defaults to the daily view and does not fetch the period rollups at all', async () => {
    const calls = stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()

    expect(wrapper.text()).toContain('Usage History')
    expect(wrapper.text()).toContain('Last 14 Days')
    expect(wrapper.text()).toContain('2026-01-05')
    // The whole point of the lazy split: an untouched tab costs nothing.
    expect(calls.periods).toBe(0)
    wrapper.unmount()
  })

  it('fetches the rollups on the first switch to Week and shows readable labels', async () => {
    const calls = stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week')

    expect(calls.periods).toBe(1)
    expect(wrapper.text()).toContain('Last 12 Weeks')
    expect(wrapper.text()).toContain('Week 03, 2026')
    expect(wrapper.text()).toContain('Week 02, 2026')
    expect(wrapper.text()).not.toContain('2026-01-05')
    wrapper.unmount()
  })

  it('shows month names rather than raw keys, and reuses the cached fetch', async () => {
    const calls = stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week')
    await selectGranularity(wrapper, 'Month')

    expect(wrapper.text()).toContain('Last 12 Months')
    expect(wrapper.text()).toContain('January 2026')
    // Both granularities arrive in one payload - switching must not refetch.
    expect(calls.periods).toBe(1)
    wrapper.unmount()
  })

  it('totals the selected view so the period spend does not have to be added up by eye', async () => {
    stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Week')

    const total = wrapper.find('.usage-total-row')
    expect(total.exists()).toBe(true)
    expect(total.text()).toContain('Total')
    expect(total.text()).toContain('10') // 8 + 2 analyses
    expect(total.text()).toContain('1.5K') // 1200 + 300 tokens, abbreviated by fmtNum
    expect(total.text()).toContain('$0.0100') // 0.008 + 0.002
    wrapper.unmount()
  })

  it('leaves the total cost as N/A when no row in the view is priced', async () => {
    stubUsageFetch(
      baseUsage(),
      basePeriods({
        weekly: [
          {
            period: '2026-W03',
            analyses: 4,
            tokens_prompt: 10,
            tokens_completion: 5,
            tokens_total: 15,
            cost: null,
          },
        ],
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Week')

    // A free/local provider must not be shown an invented $0.00 total.
    expect(wrapper.find('.usage-total-row').text()).toContain('N/A')
    wrapper.unmount()
  })

  it('reports a failed rollup fetch instead of showing an empty table', async () => {
    stubUsageFetch(baseUsage(), null, false)
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week')
    expect(wrapper.text()).toContain('Could not load weekly usage.')

    await selectGranularity(wrapper, 'Month')
    expect(wrapper.text()).toContain('Could not load monthly usage.')
    wrapper.unmount()
  })

  it('shows the per-granularity empty message when a rollup comes back empty', async () => {
    stubUsageFetch(baseUsage(), basePeriods({ weekly: [], monthly: [] }))
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week')
    expect(wrapper.text()).toContain('No analysis activity in the last 12 weeks.')
    expect(wrapper.find('.usage-total-row').exists()).toBe(false)

    await selectGranularity(wrapper, 'Month')
    expect(wrapper.text()).toContain('No analysis activity in the last 12 months.')
    wrapper.unmount()
  })

  it('refreshes the rollups on the poll only while a period view is on screen', async () => {
    vi.useFakeTimers()
    const calls = stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()

    // Day view: the poll must not touch the periods endpoint.
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(calls.periods).toBe(0)
    const usageCallsOnDay = calls.usage

    await selectGranularity(wrapper, 'Week')
    expect(calls.periods).toBe(1)

    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()
    expect(calls.periods).toBe(2)
    expect(calls.usage).toBeGreaterThan(usageCallsOnDay)
    wrapper.unmount()
  })

  it('drops the cached rollups when stats are cleared so a cleared week cannot linger', async () => {
    const calls = stubUsageFetch(baseUsage())
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Week')
    expect(calls.periods).toBe(1)

    // Drive the real button so the confirm-dialog path is exercised too.
    const confirmStore = useConfirmStore()
    const clearBtn = wrapper.findAll('button').find((b) => b.text().includes('Clear Stats'))
    await clearBtn!.trigger('click')
    await flushPromises()
    confirmStore.settle(true)
    await flushPromises()

    // Refetched, not served from the stale cache.
    expect(calls.periods).toBe(2)
    wrapper.unmount()
  })

  it('passes an unrecognised period key through untouched rather than mangling it', async () => {
    stubUsageFetch(
      baseUsage(),
      basePeriods({
        weekly: [
          { period: 'not-a-period', analyses: 1, tokens_prompt: 1, tokens_completion: 1, tokens_total: 2, cost: null },
          { period: '', analyses: 1, tokens_prompt: 1, tokens_completion: 1, tokens_total: 2, cost: null },
          { period: '2026-13', analyses: 1, tokens_prompt: 1, tokens_completion: 1, tokens_total: 2, cost: null },
        ],
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Week')

    expect(wrapper.text()).toContain('not-a-period')
    expect(wrapper.text()).toContain('2026-13') // month 13 has no name - left as-is
    expect(wrapper.text()).toContain('—') // empty key falls back to the dash
    wrapper.unmount()
  })

  it('treats a rollup row with missing counts as zero rather than rendering undefined', async () => {
    stubUsageFetch(
      baseUsage(),
      basePeriods({
        weekly: [{ period: '2026-W03', cost: null }],
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Week')

    const cells = wrapper.findAll('.usage-history-table tbody tr td').map((c) => c.text())
    expect(cells).toEqual(['Week 03, 2026', '0', '0', 'N/A'])
    wrapper.unmount()
  })

  it('discards a slow rollup response that a newer request already superseded', async () => {
    vi.useFakeTimers()
    const pending: Array<{ resolve: (v: unknown) => void; reject: () => void }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (String(url).includes('/api/ai/usage/periods')) {
          return new Promise((resolve, reject) => {
            pending.push({ resolve: (payload) => resolve(jsonResponse(payload)), reject: () => reject(new Error('x')) })
          })
        }
        return Promise.resolve(jsonResponse(baseUsage()))
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week') // request #1 - left in flight
    await vi.advanceTimersByTimeAsync(10_000) // poll fires request #2
    await flushPromises()
    expect(pending).toHaveLength(2)

    // Newest wins...
    pending[1].resolve(basePeriods({ weekly: [{ period: '2026-W09', analyses: 9, tokens_total: 90, cost: 0.9 }] }))
    await flushPromises()
    expect(wrapper.text()).toContain('Week 09, 2026')

    // ...and the older response, landing late, must not paint over it.
    pending[0].resolve(basePeriods({ weekly: [{ period: '2026-W01', analyses: 1, tokens_total: 10, cost: 0.1 }] }))
    await flushPromises()
    expect(wrapper.text()).toContain('Week 09, 2026')
    expect(wrapper.text()).not.toContain('Week 01, 2026')
    wrapper.unmount()
  })

  it('ignores a stale rollup failure so it cannot flag an error over fresher data', async () => {
    vi.useFakeTimers()
    const pending: Array<{ resolve: (v: unknown) => void; reject: () => void }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (String(url).includes('/api/ai/usage/periods')) {
          return new Promise((resolve, reject) => {
            pending.push({ resolve: (payload) => resolve(jsonResponse(payload)), reject: () => reject(new Error('x')) })
          })
        }
        return Promise.resolve(jsonResponse(baseUsage()))
      }),
    )
    const wrapper = mount(UsagePage)
    await flushPromises()

    await selectGranularity(wrapper, 'Week')
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    pending[1].resolve(basePeriods())
    await flushPromises()
    pending[0].reject() // the superseded request fails late
    await flushPromises()

    expect(wrapper.text()).not.toContain('Could not load weekly usage.')
    expect(wrapper.text()).toContain('Week 03, 2026')
    wrapper.unmount()
  })

  it('tolerates a rollup payload missing its arrays entirely', async () => {
    stubUsageFetch(baseUsage(), {})
    const wrapper = mount(UsagePage)
    await flushPromises()
    await selectGranularity(wrapper, 'Month')

    expect(wrapper.text()).toContain('No analysis activity in the last 12 months.')
    wrapper.unmount()
  })
})
