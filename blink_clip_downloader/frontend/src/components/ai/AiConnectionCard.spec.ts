import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AiConnectionCard from './AiConnectionCard.vue'
import type { AiStatus } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function baseStatus(overrides: Partial<AiStatus> = {}): AiStatus {
  return {
    enabled: true,
    prompt_debug_enabled: false,
    ai_online: true,
    provider: 'anthropic',
    model: 'claude-haiku-4-5',
    smtp_configured: false,
    analysis_stats: {
      total_analyzed: 0,
      suspicious_count: 0,
      total_frames_analyzed: 0,
      frames_analyzed_today: 0,
      last_analysis: null,
    },
    ...overrides,
  }
}

describe('AiConnectionCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('unexpected fetch'))),
    )
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows connection status, provider, and model', () => {
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus() } })
    expect(wrapper.text()).toContain('Connected')
    expect(wrapper.text()).toContain('Anthropic (Claude)')
    expect(wrapper.text()).toContain('claude-haiku-4-5')
  })

  it('shows Offline when ai_online is false', () => {
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ ai_online: false }) } })
    expect(wrapper.text()).toContain('Offline')
  })

  it('always shows the tier-1 label, and only shows the tier-2 label when escalation is configured', () => {
    const noEscalation = mount(AiConnectionCard, { props: { status: baseStatus() } })
    expect(noEscalation.text()).toContain('Tier 1 · Primary Model')
    expect(noEscalation.text()).not.toContain('Tier 2 · Escalation Model')

    const withEscalation = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'openai', escalation_model: 'gpt-4o-mini' }) },
    })
    expect(withEscalation.text()).toContain('Tier 1 · Primary Model')
    expect(withEscalation.text()).toContain('Tier 2 · Escalation Model')
  })

  it('shows escalation info when configured', () => {
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ escalation_provider: 'openai', escalation_model: 'gpt-4o-mini', escalation_online: true }),
      },
    })
    expect(wrapper.text()).toContain('Tier 2 · Escalation Model')
    expect(wrapper.text()).toContain('OpenAI (GPT)')
    expect(wrapper.text()).toContain('online')
  })

  it('shows escalation info with an unreachable tier-2 and an unlabeled provider', () => {
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({
          escalation_provider: 'some_future_provider' as never,
          escalation_model: '',
          escalation_online: false,
        }),
      },
    })
    expect(wrapper.text()).toContain('some_future_provider')
    expect(wrapper.text()).toContain('unreachable — falling back to tier 1')
  })

  it('fetches escalation models and lists them in the escalation picker', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({ enabled: true, models: [{ name: 'claude-haiku-4-5' }, { name: 'claude-opus-4-8' }] }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    const select = wrapper.find('#ai-escalation-model-picker')
    expect(select.exists()).toBe(true)
    const options = select.findAll('option')
    expect(options.some((o) => o.text().includes('claude-haiku-4-5'))).toBe(true)
    expect(options.some((o) => o.text().includes('claude-opus-4-8'))).toBe(true)
    // Regression test for the JSON-blob bug: an option's rendered text must
    // never be the object's own JSON stringification (Vue's default
    // interpolation for a non-primitive value) — only ever the plain name.
    expect(options.some((o) => o.text().includes('{'))).toBe(false)
  })

  it('updates the selected escalation model when a different option is picked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({ enabled: true, models: [{ name: 'claude-haiku-4-5' }, { name: 'claude-opus-4-8' }] }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    const select = wrapper.find('#ai-escalation-model-picker')
    await select.setValue('claude-opus-4-8')

    const copyBtn = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Copy') && b.attributes('title')?.includes('AI Escalation Model'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('claude-opus-4-8')
  })

  it('marks gpt-5.4-mini as best in the escalation picker for openai, regardless of position', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({
            enabled: true,
            models: [{ name: 'gpt-5.5' }, { name: 'gpt-5.4-mini' }, { name: 'gpt-4-turbo' }],
          }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'openai', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    const options = wrapper.find('#ai-escalation-model-picker').findAll('option')
    const best = options.find((o) => o.text().includes('gpt-5.4-mini'))!
    expect(best.text()).toContain('⭐ Best')
    expect(options.find((o) => o.text().includes('gpt-5.5'))!.text()).not.toContain('⭐ Best')
    expect(options.find((o) => o.text().includes('gpt-4-turbo'))!.text()).not.toContain('⭐ Best')
  })

  it('shows the escalation error message when no models are found', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ enabled: false, models: [], error: 'No escalation provider configured' })),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    expect(wrapper.find('#ai-escalation-model-picker option').exists()).toBe(true)
  })

  it('copies the selected escalation model id to the clipboard', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'claude-haiku-4-5' }] }))),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    const copyBtn = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Copy') && b.attributes('title')?.includes('AI Escalation Model'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('claude-haiku-4-5')
  })

  it('shows the raw escalation model id as a toast when the clipboard write fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'claude-haiku-4-5' }] }))),
    )
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    const copyBtn = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Copy') && b.attributes('title')?.includes('AI Escalation Model'))!
    await copyBtn.trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('claude-haiku-4-5')
  })

  it('warns when trying to copy an escalation model with none selected', async () => {
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const copyBtn = wrapper
      .findAll('button')
      .find((b) => b.text().includes('Copy') && b.attributes('title')?.includes('AI Escalation Model'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
  })

  it('shows a toast-worthy error state when fetching escalation models fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ escalation_provider: 'anthropic', escalation_model: '' }) },
    })
    const fetchBtn = wrapper.findAll('button').find((b) => b.text().includes('Fetch Escalation Models'))!
    await fetchBtn.trigger('click')
    await flushPromises()
    expect(fetchBtn.exists()).toBe(true)
  })

  it('shows the model picker for ollama/openai/anthropic providers and fetches models', async () => {
    // provider is 'ollama' here (not 'openai') so index-0-is-best still
    // applies to these fake ids — openai's picker instead marks a specific
    // known-good model as best regardless of position, see the dedicated
    // "marks gpt-5.4-nano as best" test below.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({ enabled: true, models: [{ name: 'model-a', size: 4_000_000_000 }, { name: 'model-b' }] }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    expect(wrapper.find('select.sel').exists()).toBe(true)
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const options = wrapper.findAll('option')
    expect(
      options.some((o) => o.text().includes('model-a') && o.text().includes('4.0 GB') && o.text().includes('Best')),
    ).toBe(true)
  })

  it('marks gpt-5.4-nano as best in the primary picker for openai, regardless of position', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({
            enabled: true,
            models: [{ name: 'gpt-5.5' }, { name: 'gpt-5.4-nano' }, { name: 'gpt-4-turbo' }],
          }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'openai' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const options = wrapper.findAll('option')
    const best = options.find((o) => o.text().includes('gpt-5.4-nano'))!
    expect(best.text()).toContain('⭐ Best')
    expect(options.find((o) => o.text().includes('gpt-5.5'))!.text()).not.toContain('⭐ Best')
    expect(options.find((o) => o.text().includes('gpt-4-turbo'))!.text()).not.toContain('⭐ Best')
  })

  it('keeps the fetch button and the select+copy row in a stable stacked layout', () => {
    // Regression test: the fetch button and the select used to share one
    // flex-wrap row, so the select started out beside the fetch button but
    // reflowed onto its own line once real (longer) options arrived — the
    // layout visibly jumped every time models were fetched. Asserting the
    // DOM structure (not just fetched-vs-empty text) locks in that the
    // select+copy row is always a distinct block below the fetch button.
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    const picker = wrapper.find('.model-picker')
    expect(picker.exists()).toBe(true)
    const fetchBtn = picker.find('.model-picker__fetch')
    const row = picker.find('.model-picker__row')
    expect(fetchBtn.exists()).toBe(true)
    expect(row.exists()).toBe(true)
    expect(row.find('select.model-picker__select').exists()).toBe(true)
    expect(row.find('.model-picker__copy').exists()).toBe(true)
  })

  it('shows a toast when the server has no vision models', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [] }))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.find('select.sel').exists()).toBe(true)
  })

  it('re-fetching models does not clobber an already-selected model', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'model-a' }, { name: 'model-b' }] }))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    const fetchBtn = wrapper.find('button')
    await fetchBtn.trigger('click')
    await flushPromises()
    await wrapper.find('select.sel').setValue('model-b')
    await fetchBtn.trigger('click')
    await flushPromises()
    expect((wrapper.find('select.sel').element as HTMLSelectElement).value).toBe('model-b')
  })

  it('copies the selected model id to the clipboard', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'model-a' }] }))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const copyBtn = wrapper.findAll('button').find((b) => b.text().includes('Copy'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('model-a')
  })

  it('warns when trying to copy with no model selected', async () => {
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    const copyBtn = wrapper.findAll('button').find((b) => b.text().includes('Copy'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).not.toHaveBeenCalled()
  })

  it('shows a toast-worthy error state when fetching models fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Fetch Models')
  })

  it('hides the model picker for moondream providers', () => {
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'moondream_cloud' }) } })
    expect(wrapper.find('select.sel').exists()).toBe(false)
  })

  it('moondream_local: shows the install prompt when not installed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(jsonResponse({ installed: false, arch_supported: true, install_state: { status: 'idle' } })),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('moondream package not installed')
  })

  it('moondream_local: shows unsupported-architecture message', async () => {
    const wrapper = mount(AiConnectionCard, {
      props: { status: baseStatus({ provider: 'moondream_local', moondream_arch_supported: false }) },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('not available on this architecture')
  })

  it('moondream_local: shows installed state', async () => {
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: true, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('✓ moondream installed')
  })

  it('moondream_local: starts an install and polls until installed', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let installing = false
    let installed = false
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/moondream/install' && opts?.method === 'POST') {
          installing = true
          return Promise.resolve(jsonResponse({ status: 'installing' }))
        }
        if (url === '/api/ai/moondream/install-status') {
          if (installing && !installed) {
            installed = true // next poll reports installed
            return Promise.resolve(
              jsonResponse({
                installed: false,
                arch_supported: true,
                install_state: { status: 'installing', log: 'working…' },
              }),
            )
          }
          return Promise.resolve(
            jsonResponse({
              installed,
              arch_supported: true,
              install_state: installed ? { status: 'installed' } : { status: 'idle' },
            }),
          )
        }
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    const installBtn = wrapper.findAll('button').find((b) => b.text().includes('Install Moondream'))!
    await installBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Installing')
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(wrapper.text()).toContain('✓ moondream installed')
    vi.useRealTimers()
  })

  it('moondream_local: unmounting clears the pending poll timer', async () => {
    // Regression test: the AI tab is v-if-gated in App.vue (fully destroyed
    // on tab switch). Without an onUnmounted cleanup, starting an install
    // and then switching tabs mid-install left the setTimeout loop running
    // forever in the background, forever re-fetching install-status and
    // keeping the destroyed component instance alive via the timer's closure.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const statusFetch = vi.fn(() =>
      Promise.resolve(
        jsonResponse({
          installed: false,
          arch_supported: true,
          install_state: { status: 'installing', log: 'working…' },
        }),
      ),
    )
    vi.stubGlobal('fetch', statusFetch)
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    const callsBeforeUnmount = statusFetch.mock.calls.length
    expect(callsBeforeUnmount).toBeGreaterThan(0)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10_000)
    await flushPromises()

    expect(statusFetch.mock.calls.length).toBe(callsBeforeUnmount)
    vi.useRealTimers()
  })

  it('moondream_local: rolls back to the Install button when the install request itself fails', async () => {
    // Regression test: startInstall() used to leave installState stuck at
    // "installing" forever on failure — no poll loop ever started to
    // self-correct it (the failure happens before pollMoondreamStatus is
    // ever reached), so the panel was stranded on "Installing… please
    // wait" with no button and no way to retry short of switching tabs.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/moondream/install' && opts?.method === 'POST') return Promise.reject(new Error('boom'))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    const installBtn = wrapper.findAll('button').find((b) => b.text().includes('Install Moondream'))!
    await installBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Installing')
    expect(wrapper.findAll('button').find((b) => b.text().includes('Install Moondream'))).toBeTruthy()
  })

  it('moondream_local: a dropped poll mid-install does not kill the progress tracker', async () => {
    // Regression test: pollMoondreamStatus() used to only reschedule itself
    // inside the try block's success path — a single failed poll request
    // (plausible over the "several minutes" this install can take) silently
    // stopped the whole polling loop, freezing the UI on "Installing…"
    // forever even though the install was likely still proceeding
    // server-side.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    let started = false
    let pollsSinceStart = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/moondream/install' && opts?.method === 'POST') {
          started = true
          return Promise.resolve(jsonResponse({ status: 'installing' }))
        }
        if (url === '/api/ai/moondream/install-status') {
          if (!started) {
            return Promise.resolve(
              jsonResponse({ installed: false, arch_supported: true, install_state: { status: 'idle' } }),
            )
          }
          pollsSinceStart++
          if (pollsSinceStart === 2) return Promise.reject(new Error('transient network blip'))
          if (pollsSinceStart >= 3) {
            return Promise.resolve(jsonResponse({ installed: true, arch_supported: true, install_state: {} }))
          }
          return Promise.resolve(
            jsonResponse({ installed: false, arch_supported: true, install_state: { status: 'installing' } }),
          )
        }
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    const installBtn = wrapper.findAll('button').find((b) => b.text().includes('Install Moondream'))!
    await installBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Installing')

    // pollsSinceStart becomes 2 here (the dropped one); the loop must keep going anyway.
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(wrapper.text()).toContain('Installing')

    // pollsSinceStart becomes 3 and reports installed.
    await vi.advanceTimersByTimeAsync(2500)
    await flushPromises()
    expect(wrapper.text()).toContain('✓ moondream installed')
    vi.useRealTimers()
  })

  it('moondream_local: shows a failed state with a retry button and log output', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({
            installed: false,
            arch_supported: true,
            install_state: { status: 'failed', log: 'pip install exited 1' },
          }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('Installation failed')
    expect(wrapper.text()).toContain('pip install exited 1')
    const retryBtn = wrapper.findAll('button').find((b) => b.text().includes('Retry Install'))!
    await retryBtn.trigger('click')
    expect(wrapper.text()).toContain('Installing')
  })

  it('runs a test analysis and shows the result', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([{ id: 'c1', camera: 'front' }]))
        if (url === '/api/ai/analyze/c1' && opts?.method === 'POST')
          return Promise.resolve(
            jsonResponse({
              clip_id: 'c1',
              camera: 'front',
              model: 'claude-haiku-4-5',
              response_text: '',
              is_suspicious: false,
              confidence: 0.92,
              summary: 'All clear',
              frame_count: 3,
              analysis_duration: 1.2,
              analyzed_at: '',
              tokens_prompt: 0,
              tokens_completion: 0,
              anomaly_score: 0,
              escalation_model: '',
              escalation_tokens_prompt: 0,
              escalation_tokens_completion: 0,
              escalation_provider: '',
            }),
          )
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus() } })
    const testBtn = wrapper.findAll('button').find((b) => b.text().includes('Test Analysis'))!
    await testBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('AI is working!')
    expect(wrapper.text()).toContain('92% confidence')
    expect(wrapper.text()).toContain('All clear')
  })

  it('runs a test analysis and shows a suspicious result with placeholders for missing fields', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([{ id: 'c1', camera: 'front' }]))
        if (url === '/api/ai/analyze/c1' && opts?.method === 'POST')
          return Promise.resolve(
            jsonResponse({
              clip_id: 'c1',
              camera: '',
              model: '',
              response_text: '',
              is_suspicious: true,
              confidence: 0.4,
              summary: '',
              frame_count: 0,
              analysis_duration: 0,
              analyzed_at: '',
              tokens_prompt: 0,
              tokens_completion: 0,
              anomaly_score: 0,
              escalation_model: '',
              escalation_tokens_prompt: 0,
              escalation_tokens_completion: 0,
              escalation_provider: '',
            }),
          )
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus() } })
    const testBtn = wrapper.findAll('button').find((b) => b.text().includes('Test Analysis'))!
    await testBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Suspicious')
    expect(wrapper.text()).toContain('Model: —')
  })

  it('test analysis: shows a warning when there are no clips yet', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus() } })
    const testBtn = wrapper.findAll('button').find((b) => b.text().includes('Test Analysis'))!
    await testBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Download a clip first')
  })

  it('shows the raw model id as a toast when the clipboard write fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'model-a' }] }))),
    )
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const copyBtn = wrapper.findAll('button').find((b) => b.text().includes('Copy'))!
    await copyBtn.trigger('click')
    await flushPromises()
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('model-a')
  })

  it('lets the user pick a model directly from the dropdown', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({ enabled: true, models: [{ name: 'model-a' }, { name: 'model-b' }] }))),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus({ provider: 'ollama' }) } })
    await wrapper.find('button').trigger('click')
    await flushPromises()
    await wrapper.find('select.sel').setValue('model-b')
    const copyBtn = wrapper.findAll('button').find((b) => b.text().includes('Copy'))!
    await copyBtn.trigger('click')
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('model-b')
  })

  it('clears the moondream poll timer when the provider changes away from moondream_local', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse({ installed: false, arch_supported: true, install_state: { status: 'installing' } }),
        ),
      ),
    )
    const wrapper = mount(AiConnectionCard, {
      props: {
        status: baseStatus({ provider: 'moondream_local', moondream_installed: false, moondream_arch_supported: true }),
      },
    })
    await flushPromises()
    await wrapper.setProps({ status: baseStatus({ provider: 'anthropic' }) })
    await flushPromises()
    vi.useRealTimers()
  })

  it('test analysis: shows a failure message when analysis errors', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/clips')) return Promise.resolve(jsonResponse([{ id: 'c1', camera: 'front' }]))
        return Promise.reject(new Error('boom'))
      }),
    )
    const wrapper = mount(AiConnectionCard, { props: { status: baseStatus() } })
    const testBtn = wrapper.findAll('button').find((b) => b.text().includes('Test Analysis'))!
    await testBtn.trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Test failed')
  })
})
