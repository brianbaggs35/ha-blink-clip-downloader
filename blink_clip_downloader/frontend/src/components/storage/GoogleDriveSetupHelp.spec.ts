import { describe, expect, it } from 'vitest'
import { DOMWrapper, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import GoogleDriveSetupHelp from './GoogleDriveSetupHelp.vue'

// PrimeVue's Dialog teleports to <body> by default — see AppSidebar.spec.ts's
// identical note for its About dialog.
function body() {
  return new DOMWrapper(document.body)
}

function mountHelp() {
  return mount(GoogleDriveSetupHelp, { global: { plugins: [PrimeVue] } })
}

describe('GoogleDriveSetupHelp', () => {
  it('renders a closed info button by default', () => {
    const wrapper = mountHelp()
    expect(wrapper.find('button').exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'Dialog' }).props('visible')).toBe(false)
  })

  it('opens the dialog on click, with a link to Google Cloud Console', async () => {
    const wrapper = mountHelp()
    await wrapper.find('button').trigger('click')

    expect(wrapper.findComponent({ name: 'Dialog' }).props('visible')).toBe(true)
    expect(body().text()).toContain('Connecting Google Drive')

    const consoleLink = body()
      .findAll('a')
      .find((a) => a.attributes('href') === 'https://console.cloud.google.com/')
    expect(consoleLink).toBeTruthy()
    expect(consoleLink!.attributes('target')).toBe('_blank')
    expect(consoleLink!.attributes('rel')).toBe('noopener')
  })

  it('calls out the "TVs and Limited Input devices" client type', async () => {
    const wrapper = mountHelp()
    await wrapper.find('button').trigger('click')
    expect(body().text()).toContain('TVs and Limited Input devices')
  })

  it('mentions moving the app to production to avoid weekly reconnects', async () => {
    const wrapper = mountHelp()
    await wrapper.find('button').trigger('click')
    expect(body().text()).toContain('In production')
  })

  it('closes when the dialog emits update:visible(false)', async () => {
    const wrapper = mountHelp()
    await wrapper.find('button').trigger('click')
    const dialog = wrapper.findComponent({ name: 'Dialog' })
    expect(dialog.props('visible')).toBe(true)

    await dialog.vm.$emit('update:visible', false)
    expect(dialog.props('visible')).toBe(false)
  })
})
