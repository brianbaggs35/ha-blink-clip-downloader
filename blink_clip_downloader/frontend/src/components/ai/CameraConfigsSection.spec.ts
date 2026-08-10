import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import CameraConfigsSection from './CameraConfigsSection.vue'
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

  it('saves description/custom_prompt while preserving is_car_camera and car_zone untouched', async () => {
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, opts?: RequestInit) => {
        if (opts?.method === 'PUT') {
          saved = JSON.parse(opts.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 1 }))
        }
        return Promise.resolve(
          jsonResponse([
            {
              camera: 'front',
              description: 'old',
              custom_prompt: '',
              is_car_camera: true,
              car_zone: { x_min: 0.1, y_min: 0.2, x_max: 0.5, y_max: 0.9 },
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
})
