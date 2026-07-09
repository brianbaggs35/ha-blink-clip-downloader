import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PromptOverlay from './PromptOverlay.vue'
import { usePromptOverlayStore } from '../../stores/promptOverlay'

describe('PromptOverlay', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('is closed by default and opens via the store', async () => {
    const wrapper = mount(PromptOverlay)
    expect(wrapper.find('.modal-bg').classes()).not.toContain('open')
    usePromptOverlayStore().show('secret prompt text')
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.modal-bg').classes()).toContain('open')
    expect(wrapper.text()).toContain('secret prompt text')
  })

  it('closes via the close button', async () => {
    const store = usePromptOverlayStore()
    store.show('x')
    const wrapper = mount(PromptOverlay)
    await wrapper.find('.modal-close').trigger('click')
    expect(store.open).toBe(false)
  })

  it('closes on backdrop click', async () => {
    const store = usePromptOverlayStore()
    store.show('x')
    const wrapper = mount(PromptOverlay)
    await wrapper.find('.modal-bg').trigger('click')
    expect(store.open).toBe(false)
  })
})
