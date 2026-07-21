import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ClipAiPanel from './ClipAiPanel.vue'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useRefreshStore } from '../../stores/refresh'
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

  it('does not re-fetch when collapsed and re-expanded', async () => {
    mockFetch({ '/api/ai/results/c1': RESULT })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click') // expand: first load
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith('/api/ai/results/c1', expect.anything())
    vi.mocked(fetch).mockClear()

    await wrapper.find('.ai-panel-hdr').trigger('click') // collapse
    await wrapper.find('.ai-panel-hdr').trigger('click') // re-expand: already loaded
    await flushPromises()
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

  it('shows an empty prompt overlay for a clip analyzed before prompt debug was ever recorded', async () => {
    mockFetch({ '/api/ai/results/c1': { ...RESULT, prompt_text: undefined }, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1', promptDebugEnabled: true } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    const promptBtn = wrapper.findAll('button').find((b) => b.text().includes('Prompt'))
    await promptBtn!.trigger('click')
    expect(usePromptOverlayStore().promptText).toBe('')
  })

  it('shows 0% (not blank) confidence for a zero-confidence result', async () => {
    mockFetch({ '/api/ai/results/c1': { ...RESULT, confidence: 0 }, '/api/ai/feedback/c1': null })
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('0% confidence')
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

  it('shows the result even when the feedback fetch itself fails', async () => {
    // getFeedbackForClip() is deliberately best-effort (.catch(() => null))
    // — a broken/500ing feedback lookup must not block the analysis result
    // itself from rendering.
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null, false))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
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

  it('"Clear" deletes the stored feedback, bumps refresh, and shows the quick-feedback prompt again', async () => {
    let deleteCalled = false
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'DELETE') {
          deleteCalled = true
          return Promise.resolve(jsonResponse({ deleted: true }))
        }
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') {
          return Promise.resolve(
            jsonResponse({
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
            }),
          )
        }
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    const tickBefore = useRefreshStore().tick
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Marked correct')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Clear')!
      .trigger('click')
    await flushPromises()
    expect(deleteCalled).toBe(true)
    expect(wrapper.text()).toContain('Was this verdict correct?')
    expect(useToastStore().message).toBe('Feedback cleared')
    expect(useRefreshStore().tick).toBeGreaterThan(tickBefore)
  })

  it('shows a toast and keeps the existing feedback when clearing fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'DELETE') return Promise.reject(new Error('down'))
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
        if (url === '/api/ai/feedback/c1') {
          return Promise.resolve(
            jsonResponse({
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
            }),
          )
        }
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
    await wrapper.find('.ai-panel-hdr').trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Clear')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Marked correct')
    expect(useToastStore().message).toBe('Failed to clear feedback')
    expect(useToastStore().isError).toBe(true)
  })

  it('opens the note form on "Incorrect" with a checkbox proposing the opposite verdict, and submits it', async () => {
    // RESULT.is_suspicious is true, so the one valid correction is "should
    // NOT have been flagged suspicious" — checking the box must send
    // corrected_suspicious: false here, not true (regression test for the
    // bug where the checkbox always proposed "suspicious" regardless of
    // the clip's current verdict).
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
    expect(wrapper.text()).toContain('Should not have been flagged suspicious')
    expect(wrapper.text()).not.toContain('Should have been flagged suspicious instead')
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
      corrected_suspicious: false,
    })
  })

  it('proposes "should have been flagged suspicious instead" when the clip is currently clear', async () => {
    const clean = { ...RESULT, is_suspicious: false }
    let submittedBody: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/feedback/c1' && opts?.method === 'POST') {
          submittedBody = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true }))
        }
        if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(clean))
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
    expect(wrapper.text()).toContain('Should have been flagged suspicious instead')
    expect(wrapper.text()).not.toContain('Should not have been flagged suspicious')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Submit')!
      .trigger('click')
    await flushPromises()
    expect(submittedBody).toEqual({
      correct: false,
      correction_note: '',
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

  it('keeps the feedback note form open and shows a toast when submitting it fails', async () => {
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
      .find((b) => b.text().includes('👎 Incorrect'))!
      .trigger('click')
    await wrapper.find('input.tag-input').setValue('it was just the mail carrier')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Submit')!
      .trigger('click')
    await flushPromises()
    expect(useToastStore().message).toBe('Failed to save feedback')
    expect(useToastStore().isError).toBe(true)
    expect(wrapper.find('input.tag-input').exists()).toBe(true)
    expect((wrapper.find('input.tag-input').element as HTMLInputElement).value).toBe('it was just the mail carrier')
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

  describe('face recognition feedback', () => {
    it('offers "Report a missed face match" when no bypass applied on this clip', async () => {
      mockFetch({ '/api/ai/results/c1': RESULT, '/api/ai/feedback/c1': null })
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Report a missed face match')
      expect(wrapper.text()).not.toContain('Wrong match')
    })

    it('offers "Wrong match" instead, naming the recognized person, when a bypass applied', async () => {
      const bypassed = { ...RESULT, face_bypass_applied: true, face_bypass_names: 'Brian' }
      mockFetch({ '/api/ai/results/c1': bypassed, '/api/ai/feedback/c1': null })
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Face match (Brian)')
      expect(wrapper.text()).toContain('Wrong match')
      expect(wrapper.text()).not.toContain('Report a missed face match')
    })

    it('submits a false_negative report with no person_name when nobody is enrolled', async () => {
      let posted: unknown
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string, opts?: RequestInit) => {
          if (url === '/api/ai/faces/feedback/c1' && opts?.method === 'POST') {
            posted = JSON.parse(opts.body as string)
            return Promise.resolve(jsonResponse({ saved: true }))
          }
          if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
          if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
          if (url === '/api/ai/faces') return Promise.resolve(jsonResponse({ available: true, faces: [] }))
          return Promise.reject(new Error(`unexpected ${url}`))
        }),
      )
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Report a missed face match'))!
        .trigger('click')
      await flushPromises()
      expect(posted).toEqual({ report_type: 'false_negative', note: '', person_name: '' })
      expect(wrapper.text()).toContain('Reported')
      // The report button is replaced by the confirmation, not left clickable.
      expect(wrapper.text()).not.toContain('Report a missed face match')
    })

    it('auto-attaches the sole enrolled name when reporting a missed match with only one person on file', async () => {
      let posted: unknown
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string, opts?: RequestInit) => {
          if (url === '/api/ai/faces/feedback/c1' && opts?.method === 'POST') {
            posted = JSON.parse(opts.body as string)
            return Promise.resolve(jsonResponse({ saved: true }))
          }
          if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
          if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
          if (url === '/api/ai/faces')
            return Promise.resolve(
              jsonResponse({ available: true, faces: [{ id: 1, name: 'Brian', created_at: '', approved: true }] }),
            )
          return Promise.reject(new Error(`unexpected ${url}`))
        }),
      )
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Report a missed face match'))!
        .trigger('click')
      await flushPromises()
      expect(posted).toEqual({ report_type: 'false_negative', note: '', person_name: 'Brian' })
      expect(wrapper.text()).toContain('Reported')
    })

    it('lets the reporter pick who was missed when more than one person is enrolled', async () => {
      let posted: unknown
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string, opts?: RequestInit) => {
          if (url === '/api/ai/faces/feedback/c1' && opts?.method === 'POST') {
            posted = JSON.parse(opts.body as string)
            return Promise.resolve(jsonResponse({ saved: true }))
          }
          if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(RESULT))
          if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
          if (url === '/api/ai/faces')
            return Promise.resolve(
              jsonResponse({
                available: true,
                faces: [
                  { id: 1, name: 'Brian', created_at: '', approved: true },
                  { id: 2, name: 'Casey', created_at: '', approved: true },
                ],
              }),
            )
          return Promise.reject(new Error(`unexpected ${url}`))
        }),
      )
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Report a missed face match'))!
        .trigger('click')
      await flushPromises()
      // Not submitted yet — a picker with both names should be showing instead.
      expect(posted).toBeUndefined()
      const select = wrapper.find('select#clip-ai-face-report-name')
      expect(select.exists()).toBe(true)
      expect(select.text()).toContain('Brian')
      expect(select.text()).toContain('Casey')
      await select.setValue('Casey')
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Submit report'))!
        .trigger('click')
      await flushPromises()
      expect(posted).toEqual({ report_type: 'false_negative', note: '', person_name: 'Casey' })
      expect(wrapper.text()).toContain('Reported')
    })

    it('cancels the missed-match person picker without submitting', async () => {
      mockFetch({
        '/api/ai/results/c1': RESULT,
        '/api/ai/feedback/c1': null,
        '/api/ai/faces': {
          available: true,
          faces: [
            { id: 1, name: 'Brian', created_at: '', approved: true },
            { id: 2, name: 'Casey', created_at: '', approved: true },
          ],
        },
      })
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Report a missed face match'))!
        .trigger('click')
      await flushPromises()
      expect(wrapper.find('select#clip-ai-face-report-name').exists()).toBe(true)
      await wrapper
        .findAll('button')
        .find((b) => b.text() === 'Cancel')!
        .trigger('click')
      expect(wrapper.find('select#clip-ai-face-report-name').exists()).toBe(false)
      expect(wrapper.text()).toContain('Report a missed face match')
    })

    it('submits a false_positive report for a bypassed clip, naming the matched person', async () => {
      let posted: unknown
      const bypassed = { ...RESULT, face_bypass_applied: true, face_bypass_names: 'Brian' }
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string, opts?: RequestInit) => {
          if (url === '/api/ai/faces/feedback/c1' && opts?.method === 'POST') {
            posted = JSON.parse(opts.body as string)
            return Promise.resolve(jsonResponse({ saved: true }))
          }
          if (url === '/api/ai/results/c1') return Promise.resolve(jsonResponse(bypassed))
          if (url === '/api/ai/feedback/c1') return Promise.resolve(jsonResponse(null))
          return Promise.reject(new Error(`unexpected ${url}`))
        }),
      )
      const wrapper = mount(ClipAiPanel, { props: { clipId: 'c1' } })
      await wrapper.find('.ai-panel-hdr').trigger('click')
      await flushPromises()
      await wrapper
        .findAll('button')
        .find((b) => b.text().includes('Wrong match'))!
        .trigger('click')
      await flushPromises()
      expect(posted).toEqual({ report_type: 'false_positive', note: '', person_name: 'Brian' })
      expect(wrapper.text()).toContain('Reported')
    })

    it('shows a toast and leaves the report button clickable when the report fails to save', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string, opts?: RequestInit) => {
          if (url === '/api/ai/faces/feedback/c1' && opts?.method === 'POST')
            return Promise.reject(new Error('network'))
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
        .find((b) => b.text().includes('Report a missed face match'))!
        .trigger('click')
      await flushPromises()
      expect(wrapper.text()).toContain('Report a missed face match')
      expect(wrapper.text()).not.toContain('Reported')
    })
  })
})
