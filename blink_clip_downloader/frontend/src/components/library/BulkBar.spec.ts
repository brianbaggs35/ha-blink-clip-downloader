import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import BulkBar from './BulkBar.vue'

describe('BulkBar', () => {
  it('shows the selected count', () => {
    const wrapper = mount(BulkBar, {
      props: { count: 3, total: 3, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    expect(wrapper.text()).toContain('3 selected')
  })

  it('emits star/delete/zip/cancel', async () => {
    // total === count so the conditional "Select all" button is hidden,
    // keeping the star/delete/zip/cancel buttons at their original indices.
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
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
    const wrapper = mount(BulkBar, {
      props: { count: 2, total: 2, zipping: true, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    const zipBtn = wrapper.findAll('button')[2]
    expect(zipBtn.attributes('disabled')).toBeDefined()
    expect(zipBtn.text()).toContain('Zipping')
  })

  it('hides the Analyze button when AI is not enabled', () => {
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    expect(wrapper.text()).not.toContain('Analyze selected')
  })

  it('shows the Analyze button when AI is enabled, and emits analyze', async () => {
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: false, aiEnabled: true, gdriveEnabled: false },
    })
    const analyzeBtn = wrapper.findAll('button').find((b) => b.text().includes('Analyze selected'))
    expect(analyzeBtn).toBeTruthy()
    await analyzeBtn!.trigger('click')
    expect(wrapper.emitted('analyze')).toHaveLength(1)
  })

  it('disables the Analyze button and shows progress text while analyzing', () => {
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: true, aiEnabled: true, gdriveEnabled: false },
    })
    const analyzeBtn = wrapper.findAll('button').find((b) => b.text().includes('Analyzing'))
    expect(analyzeBtn).toBeTruthy()
    expect(analyzeBtn!.attributes('disabled')).toBeDefined()
  })

  it('hides the Upload to Drive button when Drive is not connected', () => {
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    expect(wrapper.text()).not.toContain('Upload to Drive')
  })

  it('shows the Upload to Drive button when connected, and emits upload', async () => {
    const wrapper = mount(BulkBar, {
      props: { count: 1, total: 1, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: true },
    })
    const uploadBtn = wrapper.findAll('button').find((b) => b.text().includes('Upload to Drive'))
    expect(uploadBtn).toBeTruthy()
    await uploadBtn!.trigger('click')
    expect(wrapper.emitted('upload')).toHaveLength(1)
  })

  it('shows a "Select all" button only when some visible clips are unselected, and it emits selectAll', async () => {
    const partial = mount(BulkBar, {
      props: { count: 2, total: 5, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    const selectAllBtn = partial.findAll('button').find((b) => b.text().includes('Select all'))
    expect(selectAllBtn).toBeTruthy()
    expect(selectAllBtn!.text()).toContain('Select all 5')
    await selectAllBtn!.trigger('click')
    expect(partial.emitted('selectAll')).toHaveLength(1)

    const complete = mount(BulkBar, {
      props: { count: 5, total: 5, zipping: false, analyzing: false, aiEnabled: false, gdriveEnabled: false },
    })
    expect(complete.findAll('button').some((b) => b.text().includes('Select all'))).toBe(false)
  })
})
