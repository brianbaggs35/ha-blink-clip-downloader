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

  it('shows an explanatory message instead of a blank box when no prompt was recorded', async () => {
    const wrapper = mount(PromptOverlay)
    usePromptOverlayStore().show('')
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('No prompt was recorded')
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  // One overlay, three ways out of it. The keyboard one is why the list is
  // worth keeping together: the backdrop handles Escape itself now instead
  // of leaving it entirely to useKeyboardShortcuts' document listener, and
  // a change to the backdrop markup would otherwise drop that silently.
  it.each([
    ['the close button', '.modal-close', 'click'],
    ['a click on the backdrop', '.modal-bg', 'click'],
    ['Escape raised inside it, not only via the app-wide handler', '.modal-bg', 'keydown.escape'],
  ])('closes on %s', async (_label, selector, event) => {
    const store = usePromptOverlayStore()
    store.show('x')
    const wrapper = mount(PromptOverlay)
    await wrapper.find(selector).trigger(event)
    expect(store.open).toBe(false)
  })
})
