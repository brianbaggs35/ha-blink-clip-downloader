import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import SyncModuleCard from './SyncModuleCard.vue'
import SyncModuleCameraCard from './SyncModuleCameraCard.vue'
import type { SyncModuleInfo } from '../../api/types'

function makeModule(overrides: Partial<SyncModuleInfo> = {}): SyncModuleInfo {
  return {
    name: 'Home',
    network_id: 12345,
    serial: 'ABCDEF123',
    version: '2.13.30',
    status: 'online',
    online: true,
    armed: true,
    region_id: 'u001',
    local_storage: false,
    cameras: [
      {
        name: 'Front Door',
        armed: true,
        online: true,
        battery_state: 'ok',
        battery_level: 3,
        wifi_strength: -55,
        type: 'catalina',
      },
    ],
    ...overrides,
  }
}

function mountCard(module: SyncModuleInfo, pending = false, pendingCameras = new Set<string>()) {
  return mount(SyncModuleCard, {
    props: { module, pending, pendingCameras },
    global: { plugins: [PrimeVue] },
  })
}

describe('SyncModuleCard', () => {
  it('shows the module name, online badge, armed state, firmware, and serial', () => {
    const wrapper = mountCard(makeModule())
    expect(wrapper.text()).toContain('Home')
    expect(wrapper.text()).toContain('Online')
    expect(wrapper.text()).toContain('Armed')
    expect(wrapper.text()).toContain('Firmware 2.13.30')
    expect(wrapper.text()).toContain('Serial ABCDEF123')
    expect(wrapper.find('.sync-module-card').classes()).toContain('sync-module-armed')
  })

  it('shows Offline and Disarmed for an offline, disarmed module', () => {
    const wrapper = mountCard(makeModule({ online: false, armed: false }))
    expect(wrapper.text()).toContain('Offline')
    expect(wrapper.text()).toContain('Disarmed')
    expect(wrapper.find('.sync-module-card').classes()).toContain('sync-module-disarmed')
  })

  it('shows "Local Storage active" only when local_storage is true', () => {
    expect(mountCard(makeModule({ local_storage: true })).text()).toContain('Local Storage active')
    expect(mountCard(makeModule({ local_storage: false })).text()).not.toContain('Local Storage active')
  })

  it("omits firmware/serial lines when the backend doesn't have them", () => {
    const wrapper = mountCard(makeModule({ version: null, serial: null }))
    expect(wrapper.text()).not.toContain('Firmware')
    expect(wrapper.text()).not.toContain('Serial')
  })

  it('shows an empty-state message when the module has no cameras', () => {
    const wrapper = mountCard(makeModule({ cameras: [] }))
    expect(wrapper.text()).toContain('No cameras on this sync module')
    expect(wrapper.findComponent(SyncModuleCameraCard).exists()).toBe(false)
  })

  it('renders one SyncModuleCameraCard per camera', () => {
    const wrapper = mountCard(
      makeModule({
        cameras: [
          {
            name: 'Front Door',
            armed: true,
            online: true,
            battery_state: 'ok',
            battery_level: 3,
            wifi_strength: -55,
            type: 'catalina',
          },
          {
            name: 'Backyard',
            armed: false,
            online: true,
            battery_state: 'low',
            battery_level: 1,
            wifi_strength: -60,
            type: 'catalina',
          },
        ],
      }),
    )
    expect(wrapper.findAllComponents(SyncModuleCameraCard)).toHaveLength(2)
  })

  it('emits toggle-module when the module toggle is switched', async () => {
    const wrapper = mountCard(makeModule({ armed: true }))
    const moduleToggle = wrapper.find('.sm-module-arm input[type="checkbox"]')
    await moduleToggle.setValue(false)
    expect(wrapper.emitted('toggle-module')).toEqual([[false]])
  })

  it('emits toggle-camera with the camera name and new value when a camera card toggles', async () => {
    const wrapper = mountCard(makeModule())
    wrapper.findComponent(SyncModuleCameraCard).vm.$emit('update:armed', false)
    expect(wrapper.emitted('toggle-camera')).toEqual([['Front Door', false]])
  })

  it('disables the module toggle and shows an "Updating…" spinner instead of the armed badge while pending', () => {
    const wrapper = mountCard(makeModule({ armed: true }), true)
    expect(wrapper.find('.sm-module-arm input[type="checkbox"]').attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('Updating…')
    expect(wrapper.find('.sync-module-card').classes()).toContain('sync-module-pending')
    expect(wrapper.findComponent({ name: 'ProgressSpinner' }).exists()).toBe(true)
    expect(wrapper.find('.sm-module-arm').findComponent({ name: 'Tag' }).exists()).toBe(false)
  })

  it("passes each camera's own pending state through from the pendingCameras set", () => {
    const wrapper = mountCard(
      makeModule({
        cameras: [
          {
            name: 'Front Door',
            armed: true,
            online: true,
            battery_state: 'ok',
            battery_level: 3,
            wifi_strength: -55,
            type: 'catalina',
          },
          {
            name: 'Backyard',
            armed: true,
            online: true,
            battery_state: 'ok',
            battery_level: 3,
            wifi_strength: -55,
            type: 'catalina',
          },
        ],
      }),
      false,
      new Set(['Backyard']),
    )
    const cards = wrapper.findAllComponents(SyncModuleCameraCard)
    expect(cards[0]!.props('pending')).toBe(false)
    expect(cards[1]!.props('pending')).toBe(true)
  })
})
