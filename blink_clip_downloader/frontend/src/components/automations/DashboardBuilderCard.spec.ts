import { beforeEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import DashboardBuilderCard from './DashboardBuilderCard.vue'

function mountCard(cameras: string[] = ['Front Door', 'Back Yard']) {
  return mount(DashboardBuilderCard, { props: { cameras } })
}

describe('DashboardBuilderCard', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('starts on camera entities, with the setup sheet and the dashboard YAML', () => {
    const wrapper = mountCard()
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    // Setup sheet, dashboard view, cast script.
    expect(blocks).toHaveLength(3)
    expect(blocks[0].props('code')).toContain('/api/security-feed/snapshot/Front%20Door')
    expect(blocks[1].props('code')).toContain('camera.blink_back_yard')
  })

  it('uses every camera until specific ones are picked', async () => {
    const wrapper = mountCard()
    await wrapper.findComponent({ name: 'MultiSelect' }).vm.$emit('update:modelValue', ['Back Yard'])
    const view = wrapper.findAllComponents({ name: 'CodeBlock' })[1].props('code') as string
    expect(view).toContain('camera.blink_back_yard')
    expect(view).not.toContain('camera.blink_front_door')
  })

  it('switches to an iframe of this tab, dropping the camera setup step', async () => {
    const wrapper = mountCard()
    await wrapper.findComponent({ name: 'SelectButton' }).vm.$emit('update:modelValue', 'iframe')
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    expect(blocks).toHaveLength(2)
    expect(blocks[0].props('code')).toContain('type: iframe')
    expect(wrapper.text()).toContain('kiosk=1')
    expect(wrapper.text()).toContain('mixed content')
  })

  it('follows the add-on URL through to every generated URL', async () => {
    const wrapper = mountCard()
    await wrapper.find('#dash-url').setValue('http://blink.lan:9000')
    expect(wrapper.findAllComponents({ name: 'CodeBlock' })[0].props('code')).toContain(
      'http://blink.lan:9000/api/security-feed/snapshot/',
    )
  })

  it('drops the storage gauges when switched off', async () => {
    const wrapper = mountCard()
    const before = wrapper.findAllComponents({ name: 'CodeBlock' })[1].props('code') as string
    expect(before).toContain('type: gauge')

    await wrapper.findAllComponents({ name: 'ToggleSwitch' })[0].vm.$emit('update:modelValue', false)
    const after = wrapper.findAllComponents({ name: 'CodeBlock' })[1].props('code') as string
    expect(after).not.toContain('type: gauge')
  })

  it('drops the status card when switched off', async () => {
    const wrapper = mountCard()
    await wrapper.findAllComponents({ name: 'ToggleSwitch' })[1].vm.$emit('update:modelValue', false)
    expect(wrapper.findAllComponents({ name: 'CodeBlock' })[1].props('code')).not.toContain('type: entities')
  })

  it('threads the chosen display into the cast script', async () => {
    const wrapper = mountCard()
    await wrapper.find('#dash-player').setValue('media_player.hallway')
    expect(wrapper.findAllComponents({ name: 'CodeBlock' })[2].props('code')).toContain('media_player.hallway')
  })

  it('warns that casting needs HTTPS rather than letting it silently fail', () => {
    expect(mountCard().text()).toContain('HTTPS')
  })

  it('still renders with no cameras at all', () => {
    expect(mountCard([]).findAllComponents({ name: 'CodeBlock' })[0].props('code')).toContain('<no cameras found>')
  })

  it('follows the tiles-per-row setting into the grid card', async () => {
    const wrapper = mountCard()
    await wrapper.findComponent({ name: 'InputNumber' }).vm.$emit('update:modelValue', 4)
    expect(wrapper.findAllComponents({ name: 'CodeBlock' })[1].props('code')).toContain('columns: 4')
  })

  it('follows the view title and path into the generated view and cast script', async () => {
    const wrapper = mountCard()
    // By id rather than by index: MultiSelect's filter box is an InputText
    // too, so the positions shift with the mode.
    await wrapper.find('#dash-title').setValue('Cameras')
    await wrapper.find('#dash-path').setValue('cams')
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    expect(blocks[1].props('code')).toContain('title: Cameras')
    expect(blocks[1].props('code')).toContain('path: cams')
    expect(blocks[2].props('code')).toContain('view_path: cams')
  })

  it('switches to a YAML dashboard of its own, registration file and all', async () => {
    const wrapper = mountCard()
    // Delivery is the second SelectButton (the first picks the tile source).
    await wrapper.findAllComponents({ name: 'SelectButton' })[1].vm.$emit('update:modelValue', 'yaml-file')
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    // Setup sheet, registration, dashboard file, cast script.
    expect(blocks).toHaveLength(4)
    expect(blocks[1].props('code')).toContain('lovelace:')
    expect(blocks[1].props('code')).toContain('show_in_sidebar: true')
    expect(blocks[2].props('filename')).toBe('blink-cameras.yaml')
    expect(wrapper.text()).toContain('blink-cameras.yaml')
  })

  it('follows the dashboard path into the registration, file name and cast script', async () => {
    const wrapper = mountCard()
    await wrapper.findAllComponents({ name: 'SelectButton' })[1].vm.$emit('update:modelValue', 'yaml-file')
    await wrapper.find('#dash-path').setValue('my-cams')
    const blocks = wrapper.findAllComponents({ name: 'CodeBlock' })
    expect(blocks[1].props('code')).toContain('    my-cams:')
    expect(blocks[2].props('filename')).toBe('my-cams.yaml')
    expect(blocks[3].props('code')).toContain('dashboard_path: my-cams')
  })
})
