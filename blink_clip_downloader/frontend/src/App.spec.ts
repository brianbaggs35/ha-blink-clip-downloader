import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia } from 'pinia'
import PrimeVue from 'primevue/config'
import ToastService from 'primevue/toastservice'
import App from './App.vue'
import { useDateFilterStore } from './stores/dateFilter'

function mountApp() {
  return mount(App, { global: { plugins: [createPinia(), PrimeVue, ToastService] } })
}

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        json: () => Promise.resolve({ state: 'connected' }),
      }),
    )
    document.body.className = ''
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('applies the theme class to <body> and switches tabs via the sidebar', async () => {
    const wrapper = mountApp()
    await wrapper.vm.$nextTick()
    expect(document.body.classList.contains('dark')).toBe(true)

    expect(wrapper.find('#page-library').classes()).toContain('active')
    expect(wrapper.find('#page-automations').classes()).not.toContain('active')

    await wrapper.find('[data-tab="automations"]').trigger('click')
    expect(wrapper.find('#page-automations').classes()).toContain('active')
    expect(wrapper.find('#page-library').classes()).not.toContain('active')
    expect(wrapper.text()).toContain('HA Automation Examples')

    wrapper.unmount()
  })

  function mockArrayAwareFetch() {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        const arrayEndpoints = ['/api/cameras', '/api/activity', '/api/clips', '/api/tags', '/api/ai/camera-configs']
        let body: unknown = { state: 'connected', enabled: false }
        if (arrayEndpoints.some((p) => url.startsWith(p))) body = []
        else if (url.startsWith('/api/liveview/cameras')) body = { cameras: [] }
        else if (url.startsWith('/api/liveview/status')) body = { active: false }
        else if (url.startsWith('/api/vehicle/settings')) body = { car_description: '' }
        else if (url.startsWith('/api/ai/faces/bypass-stats')) {
          body = { total_bypassed: 0, by_name: [], recent: [] }
        } else if (url.startsWith('/api/ai/faces')) body = { available: true, faces: [] }
        return Promise.resolve({ ok: true, json: () => Promise.resolve(body), text: () => Promise.resolve('') })
      }),
    )
  }

  it('switches to the Live View tab and mounts LiveViewPage', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="liveview"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-liveview').classes()).toContain('active')
    expect(wrapper.text()).toContain('No cameras found')
    wrapper.unmount()
  })

  it('switches to the Status tab and mounts StatusPage', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="status"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-status').classes()).toContain('active')
    expect(wrapper.find('#status-grid').exists()).toBe(true)
    wrapper.unmount()
  })

  it('switches to Library when the Status tab requests a date filter', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="automations"]').trigger('click')
    expect(wrapper.find('#page-automations').classes()).toContain('active')
    useDateFilterStore().requestDate('2026-01-05')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('#page-library').classes()).toContain('active')
    wrapper.unmount()
  })

  it('does not switch tabs for a non-positive dateFilter.seq', async () => {
    // seq only ever increments from 0 via requestDate(), so seq > 0 is
    // always true in real usage — this pins down the guard itself in case
    // that invariant ever changes (e.g. a future reset action).
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="automations"]').trigger('click')
    useDateFilterStore().seq = -1
    await wrapper.vm.$nextTick()
    expect(wrapper.find('#page-automations').classes()).toContain('active')
    expect(wrapper.find('#page-library').classes()).not.toContain('active')
    wrapper.unmount()
  })

  it('switches to the AI tab', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="ai"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-ai').classes()).toContain('active')
    wrapper.unmount()
  })

  it('switches to the Usage tab', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="usage"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-usage').classes()).toContain('active')
    wrapper.unmount()
  })

  it('switches to the Models tab', async () => {
    const wrapper = mountApp()
    await wrapper.find('[data-tab="models"]').trigger('click')
    expect(wrapper.find('#page-models').classes()).toContain('active')
    expect(wrapper.text()).toContain('AI Providers')
    wrapper.unmount()
  })

  it('switches to the Vehicles tab and mounts VehiclesPage', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="vehicles"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-vehicles').classes()).toContain('active')
    expect(wrapper.text()).toContain('Protected Vehicle Description')
    wrapper.unmount()
  })

  it('switches to the Biometrics tab and mounts BiometricsPage', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await wrapper.find('[data-tab="biometrics"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('#page-biometrics').classes()).toContain('active')
    expect(wrapper.text()).toContain('stays local')
    wrapper.unmount()
  })

  it('toggling the help overlay from the sidebar opens HelpOverlay', async () => {
    const wrapper = mountApp()
    const helpOverlay = wrapper.findAll('.modal-bg').find((el) => el.text().includes('Keyboard Shortcuts'))
    expect(helpOverlay?.classes()).not.toContain('open')

    await wrapper.find('[title="Keyboard shortcuts (?)"]').trigger('click')
    expect(helpOverlay?.classes()).toContain('open')
    wrapper.unmount()
  })

  it('closes the help overlay via its own close button', async () => {
    mockArrayAwareFetch()
    const wrapper = mountApp()
    await flushPromises()
    await wrapper.find('[title="Keyboard shortcuts (?)"]').trigger('click')
    const helpOverlay = wrapper.findAll('.modal-bg').find((el) => el.text().includes('Keyboard Shortcuts'))!
    await helpOverlay.find('.modal-close').trigger('click')
    expect(helpOverlay.classes()).not.toContain('open')
    wrapper.unmount()
  })
})
