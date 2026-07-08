import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import App from './App.vue'

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
    const wrapper = mount(App, { global: { plugins: [createPinia()] } })
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

  it('toggling the help overlay from the sidebar opens HelpOverlay', async () => {
    const wrapper = mount(App, { global: { plugins: [createPinia()] } })
    const helpOverlay = wrapper
      .findAll('.modal-bg')
      .find((el) => el.text().includes('Keyboard Shortcuts'))
    expect(helpOverlay?.classes()).not.toContain('open')

    await wrapper.find('[title="Keyboard shortcuts (?)"]').trigger('click')
    expect(helpOverlay?.classes()).toContain('open')
    wrapper.unmount()
  })
})
