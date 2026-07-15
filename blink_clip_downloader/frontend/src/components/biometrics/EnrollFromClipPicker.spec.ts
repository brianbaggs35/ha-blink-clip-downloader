import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Select from 'primevue/select'
import EnrollFromClipPicker from './EnrollFromClipPicker.vue'
import type { CameraStat, ClipListItem } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function makeCamera(camera: string): CameraStat {
  return { camera, total: 1, size_bytes: 100, today: 0, this_week: 1, last_seen: '2026-01-01' }
}

function makeClip(id: string): ClipListItem {
  return {
    id,
    camera: 'Front Door',
    file_path: `/data/${id}.mp4`,
    timestamp: '2026-01-05T10:00:00Z',
    size_bytes: 1000,
    duration: 5,
    source: 'pir',
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '2026-01-05T10:01:00Z',
    archived: false,
    archive_path: '',
    notified: false,
    face_recognized: false,
  }
}

function stubRoutedFetch(opts: { cameras?: CameraStat[]; clips?: ClipListItem[]; frames?: string[] }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse(opts.cameras ?? []))
      if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: opts.frames ?? [] }))
      if (url.includes('/api/clips')) return Promise.resolve(jsonResponse(opts.clips ?? []))
      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    }),
  )
}

// A proper v-model test harness: EnrollFromClipPicker's own toggleFrame()
// reads the current selection back off the model (defineModel), so the
// handler must actually write the emitted value back as a new prop via
// wrapper.setProps() — otherwise every click would see the same stale
// (usually empty) array instead of accumulating selections.
function mountPicker(modelValue: string[] = []) {
  const wrapper: ReturnType<typeof mount> = mount(EnrollFromClipPicker, {
    props: {
      selectedFrames: modelValue,
      'onUpdate:selectedFrames': (val: string[]) => wrapper.setProps({ selectedFrames: val }),
    },
    global: { plugins: [PrimeVue] },
  })
  return wrapper
}

describe('EnrollFromClipPicker', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads cameras and shows a warning when none exist', async () => {
    stubRoutedFetch({ cameras: [] })
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.text()).toContain('No cameras found yet')
  })

  it('loads clips for the first camera and shows a thumbnail strip', async () => {
    stubRoutedFetch({ cameras: [makeCamera('Front Door')], clips: [makeClip('c1'), makeClip('c2')] })
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(2)
  })

  it('re-fetches clips when a different camera is selected', async () => {
    let clipsCallCount = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras'))
          return Promise.resolve(jsonResponse([makeCamera('Front Door'), makeCamera('Back Yard')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          clipsCallCount++
          const camera = new URL(url, 'http://x').searchParams.get('camera')
          return Promise.resolve(jsonResponse(camera === 'Back Yard' ? [makeClip('back1')] : [makeClip('front1')]))
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    expect(clipsCallCount).toBe(1)

    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Back Yard')
    await flushPromises()
    expect(clipsCallCount).toBe(2)
    expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(1)
  })

  it('does not let a stale clips response overwrite a newer camera selection', async () => {
    // Regression test: rapid camera switching had no request-sequencing
    // guard, so a slower response for an earlier selection could resolve
    // after a newer selection's response and silently overwrite it.
    let resolveFrontDoor: (() => void) | undefined
    let resolveBackYard: (() => void) | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras'))
          return Promise.resolve(jsonResponse([makeCamera('Front Door'), makeCamera('Back Yard')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          const camera = new URL(url, 'http://x').searchParams.get('camera')
          if (camera === 'Back Yard') {
            return new Promise((resolve) => {
              resolveBackYard = () => resolve(jsonResponse([makeClip('back1')]))
            })
          }
          return new Promise((resolve) => {
            resolveFrontDoor = () => resolve(jsonResponse([makeClip('front1')]))
          })
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    resolveFrontDoor?.() // initial mount's auto-selected camera load
    await flushPromises()

    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Back Yard')
    await flushPromises()
    await wrapper.findComponent(Select).vm.$emit('update:modelValue', 'Front Door')
    await flushPromises()

    expect(resolveBackYard).toBeDefined()
    expect(resolveFrontDoor).toBeDefined()

    // Resolve the latest (Front Door) request first, then the stale
    // (Back Yard) one — simulating the stale response arriving last.
    resolveFrontDoor?.()
    await flushPromises()
    resolveBackYard?.()
    await flushPromises()

    expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(1)
    expect(wrapper.find('.thumb-strip-item img').attributes('src')).toContain('front1')
  })

  it('shows a warning when the selected camera has no clips', async () => {
    stubRoutedFetch({ cameras: [makeCamera('Front Door')], clips: [] })
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.text()).toContain('No clips for this camera in that time range')
  })

  it('defaults to a 24h lookback and re-fetches with a wider since when changed', async () => {
    const clipUrls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse([makeCamera('Front Door')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          clipUrls.push(url)
          return Promise.resolve(jsonResponse([makeClip('c1')]))
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    expect(clipUrls).toHaveLength(1)
    const firstSince = new URL(clipUrls[0], 'http://x').searchParams.get('since')
    expect(firstSince).not.toBeNull()

    const selects = wrapper.findAllComponents(Select)
    await selects[1].vm.$emit('update:modelValue', 24 * 7)
    await flushPromises()
    expect(clipUrls).toHaveLength(2)
    const secondSince = new URL(clipUrls[1], 'http://x').searchParams.get('since')
    // A 7-day lookback's `since` timestamp is earlier (a smaller ISO string
    // sorts first) than the initial 24h default's.
    expect(secondSince! < firstSince!).toBe(true)
  })

  it('extracts frames for the first clip and renders a selectable grid', async () => {
    stubRoutedFetch({
      cameras: [makeCamera('Front Door')],
      clips: [makeClip('c1')],
      frames: ['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'],
    })
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.findAll('.frame-item')).toHaveLength(2)
  })

  it('shows an error message when frame extraction fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse([makeCamera('Front Door')]))
        if (url.includes('/frames')) return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse([makeClip('c1')]))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to extract frames')
  })

  it('shows a message when no frames could be extracted', async () => {
    stubRoutedFetch({ cameras: [makeCamera('Front Door')], clips: [makeClip('c1')], frames: [] })
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.text()).toContain('No frames could be extracted')
  })

  it('toggles frame selection and emits the selected list', async () => {
    stubRoutedFetch({
      cameras: [makeCamera('Front Door')],
      clips: [makeClip('c1')],
      frames: ['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'],
    })
    const wrapper = mountPicker()
    await flushPromises()
    const items = wrapper.findAll('.frame-item')
    await items[0].trigger('click')
    let emitted = wrapper.emitted('update:selectedFrames')!
    expect(emitted[emitted.length - 1][0]).toEqual(['data:image/jpeg;base64,AAA'])

    await items[1].trigger('click')
    emitted = wrapper.emitted('update:selectedFrames')!
    expect(emitted[emitted.length - 1][0]).toEqual(['data:image/jpeg;base64,AAA', 'data:image/jpeg;base64,BBB'])

    // clicking an already-selected frame deselects it
    await items[0].trigger('click')
    emitted = wrapper.emitted('update:selectedFrames')!
    expect(emitted[emitted.length - 1][0]).toEqual(['data:image/jpeg;base64,BBB'])
  })

  it('switches clips and re-fetches frames, clearing the previous selection', async () => {
    stubRoutedFetch({
      cameras: [makeCamera('Front Door')],
      clips: [makeClip('c1'), makeClip('c2')],
      frames: ['data:image/jpeg;base64,AAA'],
    })
    const wrapper = mountPicker()
    await flushPromises()
    await wrapper.findAll('.frame-item')[0].trigger('click')
    let emitted = wrapper.emitted('update:selectedFrames')!
    expect(emitted[emitted.length - 1][0]).toEqual(['data:image/jpeg;base64,AAA'])

    await wrapper.findAll('.thumb-strip-item')[1].trigger('click')
    await flushPromises()
    emitted = wrapper.emitted('update:selectedFrames')!
    expect(emitted[emitted.length - 1][0]).toEqual([])
  })

  function makeClips(n: number, prefix = 'c'): ClipListItem[] {
    return Array.from({ length: n }, (_, i) => makeClip(`${prefix}${i}`))
  }

  it('disables "older" when a page comes back short, enables it on a full page', async () => {
    // First page full (24) -> more clips exist; re-mount with a short page
    // to confirm the opposite.
    stubRoutedFetch({ cameras: [makeCamera('Front Door')], clips: makeClips(24) })
    const wrapperFull = mountPicker()
    await flushPromises()
    expect(wrapperFull.find('[aria-label="Show older clips"]').attributes('disabled')).toBeUndefined()

    stubRoutedFetch({ cameras: [makeCamera('Front Door')], clips: makeClips(5) })
    const wrapperShort = mountPicker()
    await flushPromises()
    expect(wrapperShort.find('[aria-label="Show older clips"]').attributes('disabled')).toBeDefined()
  })

  it('"newer" is disabled at offset 0 and paging older/newer round-trips the offset', async () => {
    const clipUrls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse([makeCamera('Front Door')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          clipUrls.push(url)
          return Promise.resolve(jsonResponse(makeClips(24)))
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    expect(wrapper.find('[aria-label="Show newer clips"]').attributes('disabled')).toBeDefined()
    expect(new URL(clipUrls[0], 'http://x').searchParams.get('offset')).toBe('0')

    await wrapper.find('[aria-label="Show older clips"]').trigger('click')
    await flushPromises()
    expect(new URL(clipUrls[1], 'http://x').searchParams.get('offset')).toBe('24')
    expect(wrapper.find('[aria-label="Show newer clips"]').attributes('disabled')).toBeUndefined()

    await wrapper.find('[aria-label="Show newer clips"]').trigger('click')
    await flushPromises()
    expect(new URL(clipUrls[2], 'http://x').searchParams.get('offset')).toBe('0')
  })

  it('resets the offset back to page 1 when the camera changes', async () => {
    const clipUrls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras'))
          return Promise.resolve(jsonResponse([makeCamera('Front Door'), makeCamera('Back Yard')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          clipUrls.push(url)
          const camera = new URL(url, 'http://x').searchParams.get('camera')
          return Promise.resolve(jsonResponse(camera === 'Back Yard' ? makeClips(3, 'b') : makeClips(24)))
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    await wrapper.find('[aria-label="Show older clips"]').trigger('click')
    await flushPromises()
    expect(new URL(clipUrls[1], 'http://x').searchParams.get('offset')).toBe('24')

    await wrapper.findAllComponents(Select)[0].vm.$emit('update:modelValue', 'Back Yard')
    await flushPromises()
    expect(new URL(clipUrls[2], 'http://x').searchParams.get('offset')).toBe('0')
  })

  it('paging past the last clip shows a "back to newest" recovery message', async () => {
    const clipUrls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('/api/cameras')) return Promise.resolve(jsonResponse([makeCamera('Front Door')]))
        if (url.includes('/frames')) return Promise.resolve(jsonResponse({ frames: [] }))
        if (url.includes('/api/clips')) {
          clipUrls.push(url)
          const offset = Number(new URL(url, 'http://x').searchParams.get('offset'))
          return Promise.resolve(jsonResponse(offset === 0 ? makeClips(24) : []))
        }
        return Promise.reject(new Error(`unexpected fetch: ${url}`))
      }),
    )
    const wrapper = mountPicker()
    await flushPromises()
    await wrapper.find('[aria-label="Show older clips"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('No older clips in that time range')
    const backButton = wrapper.findAll('button').find((b) => b.text().includes('Back to newest'))!
    await backButton.trigger('click')
    await flushPromises()
    expect(new URL(clipUrls[2], 'http://x').searchParams.get('offset')).toBe('0')
    expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(24)
  })
})
