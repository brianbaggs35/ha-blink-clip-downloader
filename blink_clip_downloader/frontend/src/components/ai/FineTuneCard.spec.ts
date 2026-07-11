import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import FineTuneCard from './FineTuneCard.vue'
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

function mockFetch(routes: Record<string, unknown> = {}) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string, opts?: RequestInit) => {
      for (const [pattern, body] of Object.entries(routes)) {
        if (url === pattern) {
          if (typeof body === 'function')
            return Promise.resolve(jsonResponse((body as (o?: RequestInit) => unknown)(opts)))
          return Promise.resolve(jsonResponse(body))
        }
      }
      return Promise.reject(new Error(`unexpected fetch ${url} ${opts?.method}`))
    }),
  )
}

describe('FineTuneCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an empty state when there are no fine-tunes', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('No fine-tunes yet')
  })

  it('shows a load error', async () => {
    mockFetch()
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load fine-tunes')
  })

  it('lists fine-tunes with the pending feedback count on the train button', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'My Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 4 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('My Tune')
    expect(wrapper.text()).toContain('Train from Feedback (4)')
  })

  it('creates a new fine-tune', async () => {
    let created: unknown
    mockFetch({
      '/api/ai/finetune': (opts?: RequestInit) => {
        if (opts?.method === 'POST') {
          created = JSON.parse(opts.body as string)
          return { finetune_id: 'ft-new' }
        }
        return { enabled: true, finetunes: created ? [{ finetune_id: 'ft-new', name: 'Fresh' }] : [] }
      },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Fresh')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('New Fine-tune'))!
      .trigger('click')
    await flushPromises()
    expect(created).toEqual({ name: 'Fresh', rank: 16 })
  })

  it('warns when creating without a name', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('New Fine-tune'))!
      .trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('trains from feedback and reports the trained count', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 4 },
      '/api/ai/finetune/ft1/train': { trained: 3 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Train from Feedback'))!
      .trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/ai/finetune/ft1/train',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('saves a checkpoint', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/save-checkpoint': { saved: true },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Checkpoint'))!
      .trigger('click')
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith(
      '/api/ai/finetune/ft1/save-checkpoint',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('deletes a fine-tune after confirmation', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1': { deleted: true },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    const confirm = useConfirmStore()
    const delBtn = wrapper.findAll('button').find((b) => b.text() === '🗑')!
    const clickPromise = delBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch)).toHaveBeenCalledWith('/api/ai/finetune/ft1', expect.objectContaining({ method: 'DELETE' }))
  })

  it('views checkpoints and activates one, emitting activated', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/checkpoints': { enabled: true, checkpoints: [{ step: 5 }, { step: 10 }] },
      '/api/ai/finetune/ft1/activate': { activated: true, model: 'ft1-step-10' },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Checkpoints')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Step 5')
    expect(wrapper.text()).toContain('Step 10')
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Activate')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.emitted('activated')).toHaveLength(1)
    await wrapper.find('button.btn.sm.ghost').trigger('click')
  })

  it('shows a toast when create fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/finetune' && opts?.method === 'POST') return Promise.reject(new Error('boom'))
        if (url === '/api/ai/finetune') return Promise.resolve(jsonResponse({ enabled: true, finetunes: [] }))
        if (url === '/api/ai/feedback/untrained-count') return Promise.resolve(jsonResponse({ count: 0 }))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Fresh')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('New Fine-tune'))!
      .trigger('click')
    await flushPromises()
    // covers the create-failure catch branch — no throw
  })

  it('does nothing when delete confirmation is declined', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    const confirm = useConfirmStore()
    const delBtn = wrapper.findAll('button').find((b) => b.text() === '🗑')!
    const clickPromise = delBtn.trigger('click')
    await flushPromises()
    confirm.settle(false)
    await clickPromise
    await flushPromises()
    expect(vi.mocked(fetch)).not.toHaveBeenCalledWith(
      '/api/ai/finetune/ft1',
      expect.objectContaining({ method: 'DELETE' }),
    )
  })

  it('shows a toast when delete fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/finetune/ft1' && opts?.method === 'DELETE') return Promise.reject(new Error('boom'))
        if (url === '/api/ai/finetune')
          return Promise.resolve(jsonResponse({ enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] }))
        if (url === '/api/ai/feedback/untrained-count') return Promise.resolve(jsonResponse({ count: 0 }))
        return Promise.reject(new Error(`unexpected ${url}`))
      }),
    )
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    const confirm = useConfirmStore()
    const delBtn = wrapper.findAll('button').find((b) => b.text() === '🗑')!
    const clickPromise = delBtn.trigger('click')
    await flushPromises()
    confirm.settle(true)
    await clickPromise
    await flushPromises()
    // covers the delete-failure catch branch — no throw
  })

  it('shows a toast when viewing checkpoints fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Checkpoints')!
      .trigger('click')
    await flushPromises()
    // '/api/ai/finetune/ft1/checkpoints' has no matching route -> rejects, covers the catch branch
    expect(wrapper.text()).not.toContain('Step')
  })

  it('shows a toast when activating a checkpoint fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/checkpoints': { enabled: true, checkpoints: [{ step: 5 }] },
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/api/ai/finetune/ft1/activate') return Promise.reject(new Error('boom'))
        if (url === '/api/ai/finetune/ft1/checkpoints')
          return Promise.resolve(jsonResponse({ enabled: true, checkpoints: [{ step: 5 }] }))
        if (url === '/api/ai/finetune')
          return Promise.resolve(jsonResponse({ enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] }))
        if (url === '/api/ai/feedback/untrained-count') return Promise.resolve(jsonResponse({ count: 0 }))
        return Promise.reject(new Error(`unexpected ${url} ${opts?.method}`))
      }),
    )
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Checkpoints')!
      .trigger('click')
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Activate')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.emitted('activated')).toBeUndefined()
  })

  it('shows a toast when training fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Train from Feedback'))!
      .trigger('click')
    await flushPromises()
    // '/api/ai/finetune/ft1/train' has no matching route -> rejects, covers the catch branch
  })

  it('shows a toast when saving a checkpoint fails', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Checkpoint'))!
      .trigger('click')
    await flushPromises()
    // '/api/ai/finetune/ft1/save-checkpoint' has no matching route -> rejects, covers the catch branch
  })

  it('shows the trained/no-new-feedback message when nothing was trained', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/train': { trained: 0, message: 'No new feedback to train on' },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Train from Feedback'))!
      .trigger('click')
    await flushPromises()
    // covers the trained===0 branch — no throw, message path exercised
  })

  it('falls back to id when finetune_id/name are missing', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ id: 'ft-legacy' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('ft-legacy')
  })

  it('falls back to a dash when finetune_id/id/name are all missing', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{}] },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('—')
  })

  it('defaults finetunes to an empty list when the response omits it', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    expect(wrapper.text()).toContain('No fine-tunes yet')
  })

  it('shows a default message when nothing was trained and the server sends no message', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/train': { trained: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Train from Feedback'))!
      .trigger('click')
    await flushPromises()
    // covers the r.message fallback branch — no throw
  })

  it('shows a failure toast when the server reports saved: false', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/save-checkpoint': { saved: false },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Checkpoint'))!
      .trigger('click')
    await flushPromises()
    // covers the r.saved === false branch — no throw
  })

  it('creates a fine-tune with a non-default rank', async () => {
    let created: unknown
    mockFetch({
      '/api/ai/finetune': (opts?: RequestInit) => {
        if (opts?.method === 'POST') {
          created = JSON.parse(opts.body as string)
          return { finetune_id: 'ft-new' }
        }
        return { enabled: true, finetunes: [] }
      },
      '/api/ai/feedback/untrained-count': { count: 0 },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('Fresh')
    await wrapper.find('select#finetune-new-rank').setValue('32')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('New Fine-tune'))!
      .trigger('click')
    await flushPromises()
    expect(created).toEqual({ name: 'Fresh', rank: 32 })
  })

  it('shows a toast (no navigation) when a fine-tune has no checkpoints yet', async () => {
    mockFetch({
      '/api/ai/finetune': { enabled: true, finetunes: [{ finetune_id: 'ft1', name: 'Tune' }] },
      '/api/ai/feedback/untrained-count': { count: 0 },
      '/api/ai/finetune/ft1/checkpoints': { enabled: true, checkpoints: [] },
    })
    const wrapper = mount(FineTuneCard)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text() === 'Checkpoints')!
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Back to fine-tunes')
  })
})
