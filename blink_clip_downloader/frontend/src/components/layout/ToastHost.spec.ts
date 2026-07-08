import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ToastHost from './ToastHost.vue'
import { useToastStore } from '../../stores/toast'

describe('ToastHost', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('reflects the toast store state', async () => {
    const wrapper = mount(ToastHost)
    const toast = useToastStore()
    expect(wrapper.classes()).not.toContain('show')

    toast.show('Copied to clipboard')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toBe('Copied to clipboard')
    expect(wrapper.classes()).toContain('show')
    expect(wrapper.classes()).not.toContain('err')

    toast.show('Copy failed', true)
    await wrapper.vm.$nextTick()
    expect(wrapper.classes()).toContain('err')
  })
})
