import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import Dialog from 'primevue/dialog'
import AiAnalysisConfigCard from './AiAnalysisConfigCard.vue'
import { useToastStore } from '../../stores/toast'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
    headers: new Headers(),
  } as Response
}

const CAMERA_CONFIGS = [
  {
    camera: 'Front Door',
    description: 'Front entrance',
    custom_prompt: '',
    is_car_camera: false,
    car_zone: null,
    auto_analyze: true,
  },
  {
    camera: 'Driveway',
    description: 'Driveway',
    custom_prompt: '',
    is_car_camera: true,
    car_zone: null,
    auto_analyze: false,
  },
]

function mountCard() {
  return mount(AiAnalysisConfigCard, { global: { plugins: [PrimeVue] } })
}

describe('AiAnalysisConfigCard', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('loads a summary and configures all cameras from the modal', async () => {
    let saved: unknown
    let reads = 0
    const latestConfigs = [
      { ...CAMERA_CONFIGS[0], description: 'Updated elsewhere', is_car_camera: true, auto_analyze: false },
      {
        camera: CAMERA_CONFIGS[1].camera,
        description: 'Updated driveway',
        custom_prompt: CAMERA_CONFIGS[1].custom_prompt,
        is_car_camera: CAMERA_CONFIGS[1].is_car_camera,
        car_zone: CAMERA_CONFIGS[1].car_zone,
        auto_analyze: true,
      },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, options?: RequestInit) => {
        if (options?.method === 'PUT') {
          saved = JSON.parse(options.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 2 }))
        }
        reads++
        return Promise.resolve(jsonResponse(reads === 1 ? CAMERA_CONFIGS : latestConfigs))
      }),
    )

    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('1 of 2 cameras enabled')

    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('Automatic analysis applies only to newly downloaded clips')
    expect(document.body.textContent).toContain('Front Door')
    expect(document.body.textContent).toContain('Driveway')

    const toggles = [...document.body.querySelectorAll('input[type="checkbox"]')] as HTMLInputElement[]
    expect(toggles).toHaveLength(3)
    toggles[0].click()
    await flushPromises()
    expect(toggles[1].checked).toBe(true)
    expect(toggles[2].checked).toBe(true)
    toggles[0].click()
    await flushPromises()
    toggles[1].click()
    await flushPromises()

    const saveButton = [...document.body.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('Save Settings'),
    )!
    saveButton.click()
    await flushPromises()

    expect(saved).toEqual([
      { ...latestConfigs[0], auto_analyze: true },
      { ...latestConfigs[1], auto_analyze: false },
    ])
    expect(useToastStore().message).toBe('AI analysis settings saved')
  })

  it('uses stable sanitized ids for camera toggle labels', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              ...CAMERA_CONFIGS[0],
              camera: 'Front Door / Patio',
            },
          ]),
        ),
      ),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()

    const input = document.querySelector('#ai-analysis-front-door-patio')
    const label = document.querySelector('label[for="ai-analysis-front-door-patio"]')
    expect(input).not.toBeNull()
    expect(label).not.toBeNull()
  })

  it('closes when the modal reports that it is no longer visible', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(CAMERA_CONFIGS))),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    wrapper.findComponent(Dialog).vm.$emit('update:visible', false)
    await flushPromises()
    expect(document.body.querySelector('.p-dialog')).toBeNull()
  })

  it('closes when Cancel is clicked', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse(CAMERA_CONFIGS))),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const toggles = [...document.body.querySelectorAll('input[type="checkbox"]')] as HTMLInputElement[]
    toggles[2].click()
    await flushPromises()
    const cancelButton = [...document.body.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('Cancel'),
    )!
    cancelButton.click()
    await flushPromises()
    expect(document.body.querySelector('.p-dialog')).toBeNull()
    expect(wrapper.text()).toContain('1 of 2 cameras enabled')
  })

  it('keeps a local camera when the latest server list changes', async () => {
    let reads = 0
    let saved: unknown
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, options?: RequestInit) => {
        if (options?.method === 'PUT') {
          saved = JSON.parse(options.body as string)
          return Promise.resolve(jsonResponse({ saved: true, count: 2 }))
        }
        reads++
        return Promise.resolve(
          jsonResponse(reads <= 2 ? CAMERA_CONFIGS : [{ ...CAMERA_CONFIGS[0], camera: 'Garage', auto_analyze: true }]),
        )
      }),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const saveButton = [...document.body.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('Save Settings'),
    )!
    saveButton.click()
    await flushPromises()

    expect(saved).toEqual([
      { ...CAMERA_CONFIGS[0], camera: 'Garage', auto_analyze: true },
      CAMERA_CONFIGS[0],
      CAMERA_CONFIGS[1],
    ])
  })

  it('shows an empty state when no cameras are available', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('No cameras found. Download at least one clip first.')
  })

  it('allows retrying after a camera settings load failure', async () => {
    let attempts = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(() => {
        attempts++
        return attempts === 1 ? Promise.reject(new Error('down')) : Promise.resolve(jsonResponse(CAMERA_CONFIGS))
      }),
    )
    const wrapper = mountCard()
    await flushPromises()
    expect(wrapper.text()).toContain('Unable to load camera settings.')

    await wrapper.find('button').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('Camera settings could not be loaded.')
    const retryButton = [...document.body.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('Retry'),
    )!
    retryButton.click()
    await flushPromises()
    expect(document.body.textContent).toContain('Front Door')
  })

  it('keeps the modal open and shows a toast when saving fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((_url: string, options?: RequestInit) =>
        options?.method === 'PUT' ? Promise.reject(new Error('down')) : Promise.resolve(jsonResponse(CAMERA_CONFIGS)),
      ),
    )
    const wrapper = mountCard()
    await flushPromises()
    await wrapper.find('button').trigger('click')
    await flushPromises()
    const saveButton = [...document.body.querySelectorAll('button')].find((button) =>
      button.textContent?.includes('Save Settings'),
    )!
    saveButton.click()
    await flushPromises()

    const toast = useToastStore()
    expect(toast.message).toBe('Failed to save AI analysis settings')
    expect(toast.isError).toBe(true)
    expect(document.body.textContent).toContain('AI Analysis Configuration')
  })
})
