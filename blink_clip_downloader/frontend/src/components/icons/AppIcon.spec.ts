import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AppIcon from './AppIcon.vue'

describe('AppIcon', () => {
  it('renders one <path> per path in the icon definition', () => {
    const wrapper = mount(AppIcon, { props: { name: 'brand' } })
    expect(wrapper.findAll('path')).toHaveLength(2)
    expect(wrapper.find('svg').classes()).not.toContain('filled')
  })

  it('renders circles and rects for icons that define them', () => {
    const wrapper = mount(AppIcon, { props: { name: 'theme-dark' } })
    expect(wrapper.findAll('circle')).toHaveLength(1)

    const ai = mount(AppIcon, { props: { name: 'tab-ai' } })
    expect(ai.findAll('rect')).toHaveLength(2)
  })

  it('applies the filled class for filled icons', () => {
    const wrapper = mount(AppIcon, { props: { name: 'tab-automations' } })
    expect(wrapper.find('svg').classes()).toContain('filled')
  })
})
