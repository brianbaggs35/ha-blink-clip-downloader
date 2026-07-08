import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import HelpOverlay from './HelpOverlay.vue'

describe('HelpOverlay', () => {
  it('toggles open via v-model and lists all shortcuts', () => {
    const wrapper = mount(HelpOverlay, { props: { modelValue: true } })
    expect(wrapper.classes()).toContain('open')
    expect(wrapper.text()).toContain('Play / pause')
    expect(wrapper.text()).toContain('Show / hide this overlay')
    expect(wrapper.findAll('tr')).toHaveLength(8)
  })

  it('closes via the close button, emitting update:modelValue(false)', async () => {
    const wrapper = mount(HelpOverlay, { props: { modelValue: true } })
    await wrapper.find('.modal-close').trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('closes on backdrop click', async () => {
    const wrapper = mount(HelpOverlay, { props: { modelValue: true } })
    await wrapper.trigger('click')
    expect(wrapper.emitted('update:modelValue')?.[0]).toEqual([false])
  })

  it('stays closed when modelValue is false', () => {
    const wrapper = mount(HelpOverlay, { props: { modelValue: false } })
    expect(wrapper.classes()).not.toContain('open')
  })
})
