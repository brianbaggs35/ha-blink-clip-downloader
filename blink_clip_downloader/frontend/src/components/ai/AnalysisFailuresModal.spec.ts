import { afterEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import AnalysisFailuresModal from './AnalysisFailuresModal.vue'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

describe('AnalysisFailuresModal', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    // Dialog teleports its content to document.body -- without clearing it,
    // an unmounted-but-never-explicitly-unmounted previous test's dialog
    // content lingers and pollutes body.text() assertions in later tests.
    document.body.innerHTML = ''
  })

  it('fetches the failed queue on mount', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse([])))
    vi.stubGlobal('fetch', fetchMock)
    mount(AnalysisFailuresModal)
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/ai/queue/failed', {})
  })

  it('shows a loading indicator before the fetch resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Loading')
    expect(body.text()).not.toContain('No failed analyses')
  })

  it('shows an empty state when there are no failed analyses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('No failed analyses.')
  })

  it('shows an empty state when the fetch fails, rather than crashing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('No failed analyses.')
  })

  it('lists each failure with its camera, timestamp, error message, and retry count', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              clip_id: 'c1',
              camera: 'Front Door',
              clip_path: '/clips/c1.mp4',
              error_message: 'Ollama timeout after 30s',
              completed_at: '2026-01-05T10:00:00Z',
              retry_count: 2,
            },
          ]),
        ),
      ),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Front Door')
    expect(body.text()).toContain('Ollama timeout after 30s')
    expect(body.text()).toContain(new Date('2026-01-05T10:00:00Z').toLocaleString())
    expect(body.text()).toContain('Retried 2×')
  })

  it('falls back to "Unknown error" when error_message is empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              clip_id: 'c1',
              camera: 'Backyard',
              clip_path: '/clips/c1.mp4',
              error_message: '',
              completed_at: '2026-01-05T10:00:00Z',
              retry_count: 0,
            },
          ]),
        ),
      ),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Unknown error')
  })

  it('omits the retry note when retry_count is 0', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              clip_id: 'c1',
              camera: 'Backyard',
              clip_path: '/clips/c1.mp4',
              error_message: 'boom',
              completed_at: '2026-01-05T10:00:00Z',
              retry_count: 0,
            },
          ]),
        ),
      ),
    )
    mount(AnalysisFailuresModal)
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).not.toContain('Retried')
  })

  it('emits close when the dialog is dismissed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(AnalysisFailuresModal)
    await flushPromises()
    await wrapper.findComponent({ name: 'Dialog' }).vm.$emit('update:visible', false)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
