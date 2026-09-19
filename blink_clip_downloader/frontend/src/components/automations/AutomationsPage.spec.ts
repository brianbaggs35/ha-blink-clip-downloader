import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AutomationsPage from './AutomationsPage.vue'
import * as clipsApi from '../../api/clips'

function mountPage() {
  return mount(AutomationsPage, { attachTo: document.body })
}

describe('AutomationsPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.restoreAllMocks()
    vi.spyOn(clipsApi, 'getCameras').mockResolvedValue([
      { camera: 'Front Door', total: 1, size_bytes: 1, today: 0, this_week: 0, last_seen: '' },
    ])
  })

  it('opens on the automation builder', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Suspicious clip alert')
    expect(wrapper.find('.code-block').text()).toContain('alias: "Blink – suspicious clip alert"')
  })

  it('keeps the notification channel tests on the page', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.text()).toContain('Notification Channels')
  })

  it('feeds the live camera list into the builders', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'RecipeBuilder' }).props('cameras')).toEqual(['Front Door'])
  })

  it('still renders when the camera list cannot be fetched', async () => {
    vi.spyOn(clipsApi, 'getCameras').mockRejectedValue(new Error('offline'))
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'RecipeBuilder' }).props('cameras')).toEqual([])
    expect(wrapper.text()).toContain('Suspicious clip alert')
  })

  it('offers scripts, dashboards, blueprints and the entity reference as separate tabs', async () => {
    const wrapper = mountPage()
    await flushPromises()
    const labels = wrapper.findAllComponents({ name: 'Tab' }).map((tab) => tab.text())
    expect(labels).toEqual(['Automations', 'Scripts & Helpers', 'Dashboards', 'Blueprints', 'Entities & Events'])
  })

  it('renders only the open tab, so no hidden builder is computing YAML', async () => {
    const wrapper = mountPage()
    await flushPromises()
    expect(wrapper.findComponent({ name: 'DashboardBuilderCard' }).exists()).toBe(false)

    await wrapper.findAllComponents({ name: 'Tab' })[2].trigger('click')
    await flushPromises()
    expect(wrapper.findComponent({ name: 'DashboardBuilderCard' }).exists()).toBe(true)
    expect(wrapper.findComponent({ name: 'RecipeBuilder' }).exists()).toBe(false)
  })

  it.each([
    [1, 'Sync clips now'],
    [2, 'Add one Generic Camera per Blink camera'],
    [3, 'config/blueprints/automation'],
    [4, 'blink_clip_analyzed'],
  ])('opens tab %i and renders its content', async (index, expected) => {
    const wrapper = mountPage()
    await flushPromises()
    await wrapper.findAllComponents({ name: 'Tab' })[index].trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain(expected)
  })
})
