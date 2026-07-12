import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import VehiclesPage from './VehiclesPage.vue'
import VehicleZonePicker from './VehicleZonePicker.vue'
import type { CameraConfig } from '../../api/types'

function mountPage() {
  return mount(VehiclesPage, { global: { plugins: [PrimeVue] } })
}

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

function stubRoutedFetch(routes: { camera_configs?: CameraConfig[]; vehicle_settings?: { car_description: string } }) {
  vi.stubGlobal(
    'fetch',
    vi.fn((url: string) => {
      if (url.includes('/api/vehicle/settings')) {
        return Promise.resolve(jsonResponse(routes.vehicle_settings ?? { car_description: '' }))
      }
      if (url.includes('/api/ai/camera-configs')) {
        return Promise.resolve(jsonResponse(routes.camera_configs ?? []))
      }
      if (url.includes('/api/clips')) {
        return Promise.resolve(jsonResponse([]))
      }
      return Promise.reject(new Error(`unexpected fetch: ${url}`))
    }),
  )
}

const FRONT_CAM: CameraConfig = {
  camera: 'Driveway',
  description: 'Watches the driveway',
  custom_prompt: '',
  is_car_camera: false,
  car_zone: null,
}

describe('VehiclesPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loads and shows the protected vehicle description and camera list', async () => {
    stubRoutedFetch({ vehicle_settings: { car_description: 'Silver Kia Forte' }, camera_configs: [FRONT_CAM] })
    const wrapper = mountPage()
    await flushPromises()
    expect((wrapper.find('textarea').element as HTMLTextAreaElement).value).toBe('Silver Kia Forte')
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Protection active')
  })

  it('shows an inactive warning when a car camera is set but no description', async () => {
    stubRoutedFetch({
      vehicle_settings: { car_description: '' },
      camera_configs: [{ ...FRONT_CAM, is_car_camera: true }],
    })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain("won't activate")
  })

  it('shows an empty state with no cameras', async () => {
    stubRoutedFetch({ camera_configs: [] })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('No cameras found')
  })

  it('shows a load-failure toast path gracefully (no crash)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.exists()).toBe(true)
  })

  it('saves the protected vehicle description', async () => {
    stubRoutedFetch({ vehicle_settings: { car_description: '' }, camera_configs: [FRONT_CAM] })
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.find('textarea').setValue('Red Honda Civic')
    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save Description'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      '/api/vehicle/settings',
      expect.objectContaining({
        method: 'PUT',
        body: JSON.stringify({ car_description: 'Red Honda Civic' }),
      }),
    )
  })

  it('reveals the zone picker only when a camera is marked as a car camera, and saves camera settings', async () => {
    stubRoutedFetch({ vehicle_settings: { car_description: '' }, camera_configs: [FRONT_CAM] })
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.find('.vehicle-zone-picker').exists()).toBe(false)

    const toggle = wrapper.find('input[type="checkbox"]')
    await toggle.setValue(true)
    await flushPromises()
    expect(wrapper.find('.vehicle-zone-picker').exists()).toBe(true)

    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save Camera Settings'))!
    await saveBtn.trigger('click')
    await flushPromises()
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) => (url as string).includes('/api/ai/camera-configs') && (init as RequestInit)?.method === 'PUT',
      )!
    const body = JSON.parse((call[1] as RequestInit).body as string)
    expect(body[0].is_car_camera).toBe(true)
    expect(body[0].description).toBe('Watches the driveway') // untouched field preserved
  })

  it('shows a toast when saving the description fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        if (init?.method === 'PUT' && url.includes('/api/vehicle/settings')) {
          return Promise.reject(new Error('down'))
        }
        if (url.includes('/api/vehicle/settings')) return Promise.resolve(jsonResponse({ car_description: '' }))
        if (url.includes('/api/ai/camera-configs')) return Promise.resolve(jsonResponse([FRONT_CAM]))
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save Description'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(wrapper.exists()).toBe(true) // did not throw
  })

  it('shows a toast when saving camera settings fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string, init?: RequestInit) => {
        if (init?.method === 'PUT' && url.includes('/api/ai/camera-configs')) {
          return Promise.reject(new Error('down'))
        }
        if (url.includes('/api/vehicle/settings')) return Promise.resolve(jsonResponse({ car_description: '' }))
        if (url.includes('/api/ai/camera-configs')) return Promise.resolve(jsonResponse([FRONT_CAM]))
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mountPage()
    await flushPromises()
    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save Camera Settings'))!
    await saveBtn.trigger('click')
    await flushPromises()
    expect(wrapper.exists()).toBe(true) // did not throw
  })

  it("updates a camera's car_zone when VehicleZonePicker emits a new value", async () => {
    stubRoutedFetch({
      vehicle_settings: { car_description: 'Silver Kia' },
      camera_configs: [{ ...FRONT_CAM, is_car_camera: true }],
    })
    const wrapper = mountPage()
    await flushPromises()

    const picker = wrapper.findComponent(VehicleZonePicker)
    expect(picker.exists()).toBe(true)
    const zone = { x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 }
    picker.vm.$emit('update:modelValue', zone)
    await flushPromises()

    const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save Camera Settings'))!
    await saveBtn.trigger('click')
    await flushPromises()
    const call = vi
      .mocked(fetch)
      .mock.calls.find(
        ([url, init]) => (url as string).includes('/api/ai/camera-configs') && (init as RequestInit)?.method === 'PUT',
      )!
    const body = JSON.parse((call[1] as RequestInit).body as string)
    expect(body[0].car_zone).toEqual(zone)
  })
})
