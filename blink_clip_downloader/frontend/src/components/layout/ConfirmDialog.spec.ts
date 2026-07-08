import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ConfirmDialog from './ConfirmDialog.vue'
import { useConfirmStore } from '../../stores/confirm'

describe('ConfirmDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('opens with the store title/message and resolves true on Confirm', async () => {
    const wrapper = mount(ConfirmDialog)
    const store = useConfirmStore()
    const promise = store.ask('Delete this clip?', 'Delete clip')
    await wrapper.vm.$nextTick()

    expect(wrapper.classes()).toContain('open')
    expect(wrapper.find('.modal-title').text()).toBe('Delete clip')
    expect(wrapper.text()).toContain('Delete this clip?')

    await wrapper.find('.btn.danger').trigger('click')
    await expect(promise).resolves.toBe(true)
    expect(store.open).toBe(false)
  })

  it('Cancel resolves false', async () => {
    const wrapper = mount(ConfirmDialog)
    const store = useConfirmStore()
    const promise = store.ask('Sure?')
    await wrapper.vm.$nextTick()
    await wrapper.find('.btn.ghost').trigger('click')
    await expect(promise).resolves.toBe(false)
  })

  it('clicking the backdrop resolves false', async () => {
    const wrapper = mount(ConfirmDialog)
    const store = useConfirmStore()
    const promise = store.ask('Sure?')
    await wrapper.vm.$nextTick()
    await wrapper.trigger('click')
    await expect(promise).resolves.toBe(false)
  })
})
