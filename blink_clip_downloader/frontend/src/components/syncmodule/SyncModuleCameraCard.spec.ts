import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import SyncModuleCameraCard from './SyncModuleCameraCard.vue'
import type { SyncModuleCamera } from '../../api/types'

function makeCamera(overrides: Partial<SyncModuleCamera> = {}): SyncModuleCamera {
  return {
    name: 'Front Door',
    armed: true,
    online: true,
    battery_state: 'ok',
    battery_level: 3,
    wifi_strength: -55,
    type: 'catalina',
    ...overrides,
  }
}

function mountCard(camera: SyncModuleCamera, pending = false) {
  return mount(SyncModuleCameraCard, {
    props: { camera, pending },
    global: {},
  })
}

describe('SyncModuleCameraCard', () => {
  it('shows the camera name, online badge, and armed state', () => {
    const wrapper = mountCard(makeCamera({ armed: true, online: true }))
    expect(wrapper.text()).toContain('Front Door')
    expect(wrapper.text()).toContain('Online')
    expect(wrapper.text()).toContain('Armed')
    expect(wrapper.find('.sm-cam-card').classes()).toContain('sm-cam-armed')
  })

  it('shows Offline and Disarmed for an offline, disarmed camera', () => {
    const wrapper = mountCard(makeCamera({ armed: false, online: false }))
    expect(wrapper.text()).toContain('Offline')
    expect(wrapper.text()).toContain('Disarmed')
    expect(wrapper.find('.sm-cam-card').classes()).toContain('sm-cam-disarmed')
  })

  it('shows "Battery OK" for a normal battery state', () => {
    const wrapper = mountCard(makeCamera({ battery_state: 'ok' }))
    expect(wrapper.text()).toContain('Battery OK')
  })

  it('shows "Low battery" for a low battery state', () => {
    const wrapper = mountCard(makeCamera({ battery_state: 'low' }))
    expect(wrapper.text()).toContain('Low battery')
  })

  it('omits the battery row entirely for a wired camera with no battery', () => {
    const wrapper = mountCard(makeCamera({ battery_state: null }))
    expect(wrapper.find('.sm-cam-battery').exists()).toBe(false)
  })

  it('emits update:armed with the new value when the toggle is switched', async () => {
    const wrapper = mountCard(makeCamera({ armed: true }))
    await wrapper.find('input[type="checkbox"]').setValue(false)
    expect(wrapper.emitted('update:armed')).toEqual([[false]])
  })

  it('disables the toggle and shows an "Updating…" spinner instead of the armed badge while pending', () => {
    const wrapper = mountCard(makeCamera({ armed: true }), true)
    expect(wrapper.find('input[type="checkbox"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('Updating…')
    expect(wrapper.find('.sm-cam-card').classes()).toContain('sm-cam-pending')
    expect(wrapper.findComponent({ name: 'ProgressSpinner' }).exists()).toBe(true)
    expect(wrapper.find('.sm-cam-armed-badge').exists()).toBe(false)
  })
})
