import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AutomationsPage from './AutomationsPage.vue'

describe('AutomationsPage', () => {
  it('renders the events/sensors table and all four automation snippets', () => {
    setActivePinia(createPinia())
    const wrapper = mount(AutomationsPage)
    expect(wrapper.text()).toContain('sensor.blink_downloader_status')
    expect(wrapper.text()).toContain('blink_clip_downloaded')
    expect(wrapper.text()).toContain('Blink – new clip notification')
    expect(wrapper.text()).toContain('Blink – long motion clip')
    expect(wrapper.text()).toContain('Blink – storage quota warning')
    expect(wrapper.text()).toContain('Blink – daily summary')
  })
})
