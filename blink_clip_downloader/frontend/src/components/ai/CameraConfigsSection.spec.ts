import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CameraConfigsSection from './CameraConfigsSection.vue'
import { useToastStore } from '../../stores/toast'
import { useRefreshStore } from '../../stores/refresh'

function jsonResponse(body: unknown, ok = true, headers: HeadersInit = {}) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
    headers: new Headers(headers),
  } as Response
}

describe('CameraConfigsSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows an empty state when no cameras exist', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    expect(wrapper.text()).toContain('No cameras found')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load camera configs.')
  })

  it('renders a camera with editable description/prompt fields, pointing to the Vehicles tab for car settings', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              camera: 'front',
              description: 'Driveway cam',
              custom_prompt: '',
              is_car_camera: true,
              car_zone: { x_min: 0.1, y_min: 0.2, x_max: 0.5, y_max: 0.9 },
            },
          ]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    expect((wrapper.find('input.tag-input').element as HTMLInputElement).value).toBe('Driveway cam')
    expect(wrapper.find('input[type="number"]').exists()).toBe(false)
    expect(wrapper.find('input[type="checkbox"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('Vehicles')
    // Already has a description set — the accordion header shows an
    // at-a-glance "Configured" badge without needing to open it.
    expect(wrapper.text()).toContain('Configured')
  })

  it('omits the "Configured" badge for a camera with no description or prompt set', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Configured')
  })

  it('points each camera header at its panel, even for a name with spaces', async () => {
    // aria-controls is a space-separated id list: "Front Door" used to split
    // into two ids that did not exist.
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            { camera: 'Front Door', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
          ]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection, { attachTo: document.body })
    await flushPromises()
    const controls = wrapper.find('.p-accordionheader').attributes('aria-controls')!
    expect(controls).not.toMatch(/\s/)
    expect(document.getElementById(controls)).not.toBeNull()
    wrapper.unmount()
  })

  it('expands a camera panel when its accordion header is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    expect(wrapper.find('.p-accordioncontent').isVisible()).toBe(false)
    await wrapper.find('.p-accordionheader').trigger('click')
    expect(wrapper.find('.p-accordioncontent').isVisible()).toBe(true)
  })

  it('edits the description and custom prompt', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    const inputs = wrapper.findAll('input.tag-input')
    await inputs[0].setValue('Front porch')
    await inputs[1].setValue('Watch for packages')
    expect((inputs[0].element as HTMLInputElement).value).toBe('Front porch')
    expect((inputs[1].element as HTMLInputElement).value).toBe('Watch for packages')
  })

  it('does not reload on a shared refresh tick while a local edit is unsaved', async () => {
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]),
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('edited locally, not yet saved')
    const callsBeforeTick = fetchMock.mock.calls.length

    useRefreshStore().bump()
    await flushPromises()
    expect(fetchMock.mock.calls).toHaveLength(callsBeforeTick)
  })

  it('does not overwrite an edit made while a reload was already in flight', async () => {
    // The tick watcher declines to *start* a load over a dirty form; this is
    // the other half — a form that goes dirty after one has started. Losing
    // what someone typed is worse than showing them slightly stale data.
    const rows = [{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]
    let releaseSecond: (() => void) | undefined
    let reads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        reads += 1
        if (reads === 1) return Promise.resolve(jsonResponse(rows))
        return new Promise<Response>((resolve) => {
          releaseSecond = () => resolve(jsonResponse(rows))
        })
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()

    useRefreshStore().bump()
    await flushPromises()
    expect(releaseSecond).toBeDefined()

    await wrapper.find('input.tag-input').setValue('typed while it was loading')
    releaseSecond?.()
    await flushPromises()

    expect((wrapper.find('input.tag-input').element as HTMLInputElement).value).toBe('typed while it was loading')
  })

  it('does not surface an error from a superseded reload', async () => {
    const rows = [{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]
    const pending: { resolve: (v: Response) => void; reject: (e: Error) => void }[] = []
    let reads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        reads += 1
        if (reads === 1) return Promise.resolve(jsonResponse(rows))
        return new Promise<Response>((resolve, reject) => pending.push({ resolve, reject }))
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()

    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()
    expect(pending).toHaveLength(2)

    pending[1].resolve(jsonResponse(rows))
    await flushPromises()
    pending[0].reject(new Error('down'))
    await flushPromises()

    expect(wrapper.text()).not.toContain('Could not load')
  })

  it('ignores a superseded reload that answers last', async () => {
    const rows = [{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]
    const pending: ((v: Response) => void)[] = []
    let reads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        reads += 1
        if (reads === 1) return Promise.resolve(jsonResponse(rows))
        return new Promise<Response>((resolve) => pending.push(resolve))
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()

    useRefreshStore().bump()
    await flushPromises()
    useRefreshStore().bump()
    await flushPromises()

    pending[1](jsonResponse([{ ...rows[0], description: 'Newest' }]))
    await flushPromises()
    pending[0](jsonResponse([{ ...rows[0], description: 'Stale' }]))
    await flushPromises()

    expect((wrapper.find('input.tag-input').element as HTMLInputElement).value).toBe('Newest')
  })

  it('falls back to the local auto_analyze preference when the server omits it', async () => {
    let saved: unknown
    let reads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        reads++
        return Promise.resolve(
          jsonResponse([
            {
              camera: 'front',
              description: '',
              custom_prompt: '',
              is_car_camera: false,
              car_zone: null,
              // Present (true) on the initial load, but missing from the
              // save-time snapshot -- e.g. a config file written before
              // auto_analyze existed. The merge must keep the
              // already-loaded local value rather than treat this as
              // "explicitly disabled."
              ...(reads === 1 ? { auto_analyze: true } : {}),
            },
          ]),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()

    expect((saved as Array<Record<string, unknown>>)[0]).toMatchObject({ camera: 'front', auto_analyze: true })
  })

  it('saves description/custom_prompt while preserving is_car_camera and car_zone untouched', async () => {
    let saved: unknown
    let reads = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        reads++
        return Promise.resolve(
          jsonResponse([
            {
              camera: 'front',
              description: reads === 1 ? 'old' : 'newer server description',
              custom_prompt: '',
              is_car_camera: true,
              car_zone: { x_min: 0.1, y_min: 0.2, x_max: 0.5, y_max: 0.9 },
              ...(reads === 1 ? {} : { auto_analyze: false }),
            },
          ]),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('new description')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()
    expect(saved).toEqual([
      {
        camera: 'front',
        description: 'new description',
        custom_prompt: '',
        is_car_camera: true,
        car_zone: { x_min: 0.1, y_min: 0.2, x_max: 0.5, y_max: 0.9 },
        auto_analyze: false,
      },
    ])
  })

  it('merges a local description edit into the renamed remote entry when the alias header identifies the rename', async () => {
    let reads = 0
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        reads++
        if (reads === 1) {
          return Promise.resolve(
            jsonResponse([
              {
                camera: 'Front Door',
                description: 'old',
                custom_prompt: '',
                is_car_camera: false,
                car_zone: null,
                auto_analyze: true,
              },
            ]),
          )
        }
        return Promise.resolve(
          jsonResponse(
            [
              {
                camera: 'Entryway',
                description: '',
                custom_prompt: '',
                is_car_camera: false,
                car_zone: null,
                auto_analyze: true,
              },
            ],
            true,
            { 'X-Camera-Aliases': JSON.stringify({ 'front door': 'Entryway' }) },
          ),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper.find('input.tag-input').setValue('edited locally')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()

    expect(saved).toEqual([
      {
        camera: 'Entryway',
        description: 'edited locally',
        custom_prompt: '',
        is_car_camera: false,
        car_zone: null,
        auto_analyze: true,
      },
    ])
  })

  it('shows a toast on save failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()
    const toast = useToastStore()
    expect(toast.message).toBe('Failed to save camera configs')
    expect(toast.isError).toBe(true)
  })

  it('keeps local cameras when the latest server list changes', async () => {
    let reads = 0
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 2 }))
        }
        reads++
        return Promise.resolve(
          jsonResponse(
            reads === 1
              ? [{ camera: 'front', description: 'old', custom_prompt: '', is_car_camera: false, car_zone: null }]
              : [{ camera: 'garage', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }],
          ),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()

    expect(saved).toEqual([
      { camera: 'garage', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
      {
        camera: 'front',
        description: 'old',
        custom_prompt: '',
        is_car_camera: false,
        car_zone: null,
        auto_analyze: true,
      },
    ])
  })

  it('declines to guess a rename when the server list changed ambiguously (two unmatched cameras on each side)', async () => {
    let reads = 0
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 2 }))
        }
        reads++
        if (reads === 1) {
          return Promise.resolve(
            jsonResponse([
              { camera: 'Front Door', description: 'old', custom_prompt: '', is_car_camera: false, car_zone: null },
              { camera: 'Backyard', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
            ]),
          )
        }
        // Two cameras this component has never seen before -- with two
        // local-only *and* two latest-only entries, the "exactly one
        // renamed camera" heuristic must not guess which maps to which,
        // so both server entries pass through unmerged.
        return Promise.resolve(
          jsonResponse([
            { camera: 'Entryway', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
            { camera: 'Garage', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
          ]),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection)
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()

    expect(saved).toEqual([
      { camera: 'Entryway', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
      { camera: 'Garage', description: '', custom_prompt: '', is_car_camera: false, car_zone: null },
      {
        camera: 'Front Door',
        description: 'old',
        custom_prompt: '',
        is_car_camera: false,
        car_zone: null,
        auto_analyze: true,
      },
      {
        camera: 'Backyard',
        description: '',
        custom_prompt: '',
        is_car_camera: false,
        car_zone: null,
        auto_analyze: true,
      },
    ])
  })
})
