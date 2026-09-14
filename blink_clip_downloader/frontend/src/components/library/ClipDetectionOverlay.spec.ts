import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ClipDetectionOverlay from './ClipDetectionOverlay.vue'
import type { DetectedBox } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function box(overrides: Partial<DetectedBox> = {}): DetectedBox {
  return {
    label: 'person',
    confidence: 0.9,
    track_id: 1,
    offset_seconds: 0,
    box: [0.1, 0.2, 0.3, 0.8],
    ...overrides,
  }
}

async function mountOverlay(objects: DetectedBox[], currentTime = 0, ok = true) {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(jsonResponse({ objects }, ok))),
  )
  const wrapper = mount(ClipDetectionOverlay, { props: { clipId: 'c1', currentTime } })
  await flushPromises()
  return wrapper
}

describe('ClipDetectionOverlay', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('draws a box for each detection at the current moment', async () => {
    const wrapper = await mountOverlay([box(), box({ label: 'car', track_id: 2 })])
    const rects = wrapper.findAll('rect')
    expect(rects).toHaveLength(2)
    expect(rects[0].attributes('x')).toBe('10')
    expect(rects[0].attributes('width')).toBe(String(0.2 * 100))
    expect(rects[0].classes()).toContain('detection-person')
    expect(rects[1].classes()).toContain('detection-vehicle')
  })

  it('labels each box with its class', async () => {
    const wrapper = await mountOverlay([box({ label: 'dog' })])
    const label = wrapper.find('.detection-label')
    expect(label.text()).toBe('dog')
    expect(label.classes()).toContain('detection-other')
  })

  it('shows only the detections near the current playback position', async () => {
    // Sampled seconds apart, so each box owns the window around its own
    // timestamp — otherwise the overlay is blank for most of the clip.
    const objects = [
      box({ offset_seconds: 0 }),
      box({ offset_seconds: 2, track_id: 2 }),
      box({ offset_seconds: 4, track_id: 3 }),
    ]
    const wrapper = await mountOverlay(objects, 2)
    expect(wrapper.findAll('rect')).toHaveLength(1)
    expect(wrapper.find('.detection-label').attributes('style')).toContain('left: 10%')
    await wrapper.setProps({ currentTime: 0.2 })
    expect(wrapper.findAll('rect')).toHaveLength(1)
    // Past the last sampled frame there is nothing to draw at all.
    await wrapper.setProps({ currentTime: 20 })
    expect(wrapper.findAll('rect')).toHaveLength(0)
  })

  it('falls back to a default window when every box shares one timestamp', async () => {
    const wrapper = await mountOverlay([box(), box({ track_id: 2 })], 0.9)
    expect(wrapper.findAll('rect')).toHaveLength(2)
  })

  it('renders nothing when the clip has no stored boxes', async () => {
    const wrapper = await mountOverlay([])
    expect(wrapper.find('[data-testid="detection-overlay"]').exists()).toBe(false)
  })

  it('stays silent when the request fails', async () => {
    // The overlay is an extra; a modal that pops an error because an
    // optional decoration failed is worse than one without it.
    const wrapper = await mountOverlay([], 0, false)
    expect(wrapper.find('[data-testid="detection-overlay"]').exists()).toBe(false)
  })

  it('tolerates a response with no objects array', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({}))),
    )
    const wrapper = mount(ClipDetectionOverlay, { props: { clipId: 'c1', currentTime: 0 } })
    await flushPromises()
    expect(wrapper.find('[data-testid="detection-overlay"]').exists()).toBe(false)
  })

  it('clamps an inverted box rather than rendering a negative size', async () => {
    const wrapper = await mountOverlay([box({ box: [0.6, 0.6, 0.2, 0.2] })])
    expect(wrapper.find('rect').attributes('width')).toBe('0')
    expect(wrapper.find('rect').attributes('height')).toBe('0')
  })

  it('ignores a slow fetch for a clip the modal has already moved off', async () => {
    // Stepping to the next clip with the overlay on would otherwise paint
    // the previous clip's boxes over the current video.
    const resolvers: ((value: Response) => void)[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve) => resolvers.push(resolve))),
    )
    const wrapper = mount(ClipDetectionOverlay, {
      props: { clipId: 'c1', currentTime: 0 },
    })
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()
    expect(resolvers).toHaveLength(2)

    resolvers[1](jsonResponse({ objects: [box({ label: 'car' })] }))
    await flushPromises()
    resolvers[0](jsonResponse({ objects: [box(), box(), box()] }))
    await flushPromises()

    expect(wrapper.findAll('rect')).toHaveLength(1)
    expect(wrapper.find('.detection-label').text()).toBe('car')
  })

  it('keeps the open clip’s boxes when a stale fetch fails', async () => {
    // Same reasoning as the stale-success case above: the failure belongs
    // to a clip the modal has already left, so clearing the boxes would
    // wipe the overlay off the clip actually on screen.
    const pending: { resolve: (value: Response) => void; reject: (reason: Error) => void }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise<Response>((resolve, reject) => pending.push({ resolve, reject }))),
    )
    const wrapper = mount(ClipDetectionOverlay, {
      props: { clipId: 'c1', currentTime: 0 },
    })
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()

    pending[1].resolve(jsonResponse({ objects: [box({ label: 'car' })] }))
    await flushPromises()
    pending[0].reject(new Error('down'))
    await flushPromises()

    expect(wrapper.findAll('rect')).toHaveLength(1)
    expect(wrapper.find('.detection-label').text()).toBe('car')
  })

  it('reloads when the clip changes', async () => {
    const wrapper = await mountOverlay([box()])
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.map((c) => c[0])).toEqual(['/api/ai/detections/c1', '/api/ai/detections/c2'])
  })
})
