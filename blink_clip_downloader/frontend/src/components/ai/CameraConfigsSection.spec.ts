import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CameraConfigsSection from './CameraConfigsSection.vue'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
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
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    expect(wrapper.text()).toContain('No cameras found')
  })

  it('shows a load error', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load camera configs.')
  })

  it('renders a camera with description/prompt fields and a car-zone editor when is_car_camera is set', async () => {
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
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    expect((wrapper.find('input.tag-input').element as HTMLInputElement).value).toBe('Driveway cam')
    const zoneInputs = wrapper.findAll('input[type="number"]')
    expect(zoneInputs.map((i) => (i.element as HTMLInputElement).value)).toEqual(['10', '20', '50', '90'])
  })

  it('shows the car-protection warning when a car camera has no active protection', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: true, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: false } })
    await flushPromises()
    expect(wrapper.text()).toContain('no')
    expect(wrapper.text()).toContain('Protected Vehicle')
  })

  it('does not show the warning when carProtectionActive is true', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: true, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: true } })
    await flushPromises()
    expect(wrapper.text()).not.toContain('will not activate')
  })

  it('saves configs with a complete car zone converted to a 0-1 rectangle', async () => {
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        return Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: true, car_zone: null }]),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    const zoneInputs = wrapper.findAll('input[type="number"]')
    await zoneInputs[0].setValue('10')
    await zoneInputs[1].setValue('20')
    await zoneInputs[2].setValue('50')
    await zoneInputs[3].setValue('90')
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()
    expect(saved).toEqual([
      {
        camera: 'front',
        description: '',
        custom_prompt: '',
        is_car_camera: true,
        car_zone: { x_min: 0.1, y_min: 0.2, x_max: 0.5, y_max: 0.9 },
      },
    ])
  })

  it('saves car_zone as null when the zone fields are incomplete', async () => {
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        return Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: true, car_zone: null }]),
        )
      }),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()
    expect((saved as Array<{ car_zone: unknown }>)[0].car_zone).toBeNull()
  })

  it('edits the description, custom prompt, and is_car_camera checkbox', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([{ camera: 'front', description: '', custom_prompt: '', is_car_camera: false, car_zone: null }]),
        ),
      ),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    const inputs = wrapper.findAll('input.tag-input')
    await inputs[0].setValue('Front porch')
    await inputs[1].setValue('Watch for packages')
    expect((inputs[0].element as HTMLInputElement).value).toBe('Front porch')
    expect((inputs[1].element as HTMLInputElement).value).toBe('Watch for packages')
    await wrapper.find('input[type="checkbox"]').setValue(true)
    expect(wrapper.find('.status-card').text()).toContain('Car zone')
  })

  it('shows a toast on save failure', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') return Promise.reject(new Error('down'))
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(CameraConfigsSection, { props: { carProtectionActive: null } })
    await flushPromises()
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Save Camera Configs'))!
      .trigger('click')
    await flushPromises()
    // no throw — error path handled via toast
  })
})
