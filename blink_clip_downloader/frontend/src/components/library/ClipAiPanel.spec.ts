import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ClipAiPanel from './ClipAiPanel.vue'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useRefreshStore } from '../../stores/refresh'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const RESULT = {
  clip_id: 'c1',
  camera: 'front',
  model: 'claude-haiku-4-5',
  response_text: 'raw text',
  is_suspicious: true,
  confidence: 0.87,
  summary: 'Someone at the door',
  frame_count: 4,
  analysis_duration: 2,
  analyzed_at: '2026-01-05T10:00:00Z',
  tokens_prompt: 100,
  tokens_completion: 50,
  anomaly_score: 0.5,
  escalation_model: '',
  escalation_tokens_prompt: 0,
  escalation_tokens_completion: 0,
  escalation_provider: '',
  prompt_text: 'the actual prompt',
}

function mockFetch(routes: Record<string, unknown>) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      for (const [pattern, body] of Object.entries(routes)) {
        if (url.startsWith(pattern)) {
          if (typeof body === 'function')
            return Promise.resolve(jsonResponse((body as (o?: RequestInit) => unknown)(opts)))
          return Promise.resolve(jsonResponse(body))
        }
      }
      return Promise.reject(new Error(`unexpected fetch ${url}`))
    }),
  )
}

describe('ClipAiPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('starts collapsed and does not fetch until expanded', () => {
    mockFetch({ '/api/ai/results/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    expect(wrapper.find('.ai-panel-body').classes()).not.toContain('open')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('shows "Not analyzed yet" and an Analyze Now button when there is no result', async () => {
    mockFetch({ '/api/ai/results/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Not analyzed yet')
    expect(wrapper.text()).toContain('Analyze Now')
  })

  it('renders a suspicious result with confidence, summary, and feedback prompt', async () => {
    mockFetch({ '/api/ai/results/c1': RESULT, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Suspicious')
    expect(wrapper.text()).toContain('87% confidence')
    expect(wrapper.text()).toContain('Someone at the door')
    expect(wrapper.text()).toContain('Was this verdict correct?')
  })

  it('shows existing feedback verdict instead of the prompt buttons', async () => {
    mockFetch({
      '/api/ai/results/c1': RESULT,
      '/api/ai/feedback/c1': {
        id: 1,
        clip_id: 'c1',
        camera: 'front',
        analysis_result_id: 1,
        original_suspicious: true,
        original_confidence: 0.87,
        correct: false,
        correction_note: 'false alarm',
        corrected_suspicious: false,
        created_at: '',
        trained_at: '',
      },
    })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Marked incorrect')
    expect(wrapper.text()).toContain('false alarm')
  })

  it('toggles the raw response block', async () => {
    mockFetch({ '/api/ai/results/c1': RESULT, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('raw text')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Full response'))!
      .trigger('click')
    expect(wrapper.text()).toContain('raw text')
  })

  it('only shows the Prompt button when promptDebugEnabled is true', async () => {
    mockFetch({ '/api/ai/results/c1': RESULT, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1', promptDebugEnabled: true } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    const promptBtn = wrapper.findAll('button').find((b) => b.text().includes('Prompt'))
    expect(promptBtn).toBeTruthy()
    await promptBtn!.trigger('click')
    expect(usePromptOverlayStore().promptText).toBe('the actual prompt')
  })

  it('re-analyzes and reloads the result', async () => {
    let analyzed = false
    mockFetch({
      '/api/ai/analyze/c1': () => {
        analyzed = true
        return RESULT
      },
      '/api/ai/results/c1': () => (analyzed ? RESULT : null),
      '/api/ai/feedback/c1': null,
    })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Not analyzed yet')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Analyze Now'))!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('87% confidence')
  })

  it('shows a load error when fetching the result fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load analysis')
  })

  it('shows a toast and lets the user retry when analyze fails on a never-analyzed clip', async () => {
    // Regression test: analyzeNow()'s catch used to set loadError, which
    // permanently stranded the panel on "Failed to load analysis" — loaded
    // stays true from the earlier successful (empty) load, so toggle()'s
    // `!loaded.value` guard never re-fetches, and there was no way back to
    // the retry button short of remounting the whole component.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/ai/analyze/c1') return Promise.reject(new Error('boom'))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Analyze Now'))!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Failed to load analysis')
    expect(wrapper.text()).toContain('Not analyzed yet')
    expect(wrapper.findAll('button').find((b) => b.text().includes('Analyze Now'))).toBeTruthy()
  })

  it('keeps showing the existing result if a re-analyze attempt fails', async () => {
    // Regression test: a transient re-analyze failure must not hide the
    // still-valid previous result behind a blanket "Failed to load
    // analysis" message — nothing changed server-side, so the old result
    // is still accurate.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/ai/analyze/c1') return Promise.reject(new Error('boom'))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('87% confidence')

    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Re-analyze'))!
      .trigger('click')
    await flushPromises()

    expect(wrapper.text()).not.toContain('Failed to load analysis')
    expect(wrapper.text()).toContain('87% confidence')
  })

  it('renders a clean (non-suspicious) result without summary/analyzed_at/frame_count', async () => {
    const clean = { ...RESULT, is_suspicious: false, summary: '', analyzed_at: '', frame_count: 0 }
    mockFetch({ '/api/ai/results/c1': clean, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.find('.ai-badge-clean').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('frame(s) analyzed')
  })

  it('shows the "Marked correct" verdict when feedback.correct is true', async () => {
    mockFetch({
      '/api/ai/results/c1': RESULT,
      '/api/ai/feedback/c1': {
        id: 1,
        clip_id: 'c1',
        camera: 'front',
        analysis_result_id: 1,
        original_suspicious: true,
        original_confidence: 0.87,
        correct: true,
        correction_note: '',
        corrected_suspicious: null,
        created_at: '',
        trained_at: '',
      },
    })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Marked correct')
  })

  it('"Change" clears feedback and shows the quick-feedback prompt again', async () => {
    mockFetch({
      '/api/ai/results/c1': RESULT,
      '/api/ai/feedback/c1': {
        id: 1,
        clip_id: 'c1',
        camera: 'front',
        analysis_result_id: 1,
        original_suspicious: true,
        original_confidence: 0.87,
        correct: true,
        correction_note: '',
        corrected_suspicious: null,
        created_at: '',
        trained_at: '',
      },
    })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Change')!
      .trigger('click')
    expect(wrapper.text()).toContain('Was this verdict correct?')
  })

  it('opens the note form on "Incorrect", submits it with the corrected-suspicious checkbox, and reloads', async () => {
    let submittedBody: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST') {
          submittedBody = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true }))
        }
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👎 Incorrect'))!
      .trigger('click')
    expect(wrapper.find('input.tag-input').exists()).toBe(true)
    await wrapper.find('input.tag-input').setValue('it was just the mail carrier')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Submit')!
      .trigger('click')
    await flushPromises()
    expect(submittedBody).toEqual({
      correct: false,
      correction_note: 'it was just the mail carrier',
      corrected_suspicious: true,
    })
  })

  it('submits the feedback note without checking corrected-suspicious', async () => {
    let submittedBody: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST') {
          submittedBody = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true }))
        }
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👎 Incorrect'))!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Submit')!
      .trigger('click')
    await flushPromises()
    expect(submittedBody).toEqual({ correct: false, correction_note: '', corrected_suspicious: undefined })
  })

  it('falls back to placeholders when model and response text are missing', async () => {
    mockFetch({ '/api/ai/results/c1': { ...RESULT, model: '', response_text: '' }, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Model: —')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Full response'))!
      .trigger('click')
    expect(wrapper.text()).toContain('📄 Hide response')
  })

  it('cancels the feedback note form', async () => {
    mockFetch({ '/api/ai/results/c1': RESULT, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👎 Incorrect'))!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Cancel')!
      .trigger('click')
    expect(wrapper.find('input.tag-input').exists()).toBe(false)
  })

  it('shows a toast and keeps the prompt when feedback submission fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST') return Promise.reject(new Error('down'))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👍 Correct'))!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Was this verdict correct?')
  })

  it('submits quick "correct" feedback and reloads', async () => {
    mockFetch({
      '/api/ai/results/c1': RESULT,
      '/api/ai/feedback/c1': null,
      '/api/ai/feedback/c1|POST': { saved: true },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST')
          return Promise.resolve(jsonResponse({ saved: true }))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👍 Correct'))!
      .trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/ai/feedback/c1', expect.objectContaining({ method: 'POST' }))
  })

  it('bumps the shared refresh store on a successful feedback submission', async () => {
    // Regression test: this panel can be opened (via the clip modal) from
    // the AI tab's Suspicious Activity Feed without switching tabs — without
    // this, AdaptiveLearningCard's accuracy stats and SuspiciousFeed's own
    // list had no way to learn that feedback changed and stayed stale.
    mockFetch({
      '/api/ai/results/c1': RESULT,
      '/api/ai/feedback/c1': null,
      '/api/ai/feedback/c1|POST': { saved: true },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST')
          return Promise.resolve(jsonResponse({ saved: true }))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    const tickBefore = useRefreshStore().tick
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('👍 Correct'))!
      .trigger('click')
    await flushPromises()
    expect(useRefreshStore().tick).toBeGreaterThan(tickBefore)
  })
})
