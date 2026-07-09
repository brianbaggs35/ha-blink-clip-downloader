import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import BulkBar from './BulkBar.vue'

describe('BulkBar', () => {
  it('shows the selected count', () => {
    const wrapper = mount(BulkBar, { props: { count: 3, zipping: false } })
    expect(wrapper.text()).toContain('3 selected')
  })

  it('emits star/delete/zip/cancel', async () => {
    const wrapper = mount(BulkBar, { props: { count: 1, zipping: false } })
    await wrapper.find('button:nth-of-type(1)').trigger('click')
    await wrapper.find('button:nth-of-type(2)').trigger('click')
    await wrapper.find('button:nth-of-type(3)').trigger('click')
    await wrapper.find('button:nth-of-type(4)').trigger('click')
    expect(wrapper.emitted('star')).toHaveLength(1)
    expect(wrapper.emitted('delete')).toHaveLength(1)
    expect(wrapper.emitted('zip')).toHaveLength(1)
    expect(wrapper.emitted('cancel')).toHaveLength(1)
  })

  it('disables the ZIP button and shows progress text while zipping', () => {
    const wrapper = mount(BulkBar, { props: { count: 2, zipping: true } })
    const zipBtn = wrapper.findAll('button')[2]
    expect(zipBtn.attributes('disabled')).toBeDefined()
    expect(zipBtn.text()).toContain('Zipping')
  })
})
