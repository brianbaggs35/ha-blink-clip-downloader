import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import ToggleSwitch from 'primevue/toggleswitch'
import AssetCameraCard from './AssetCameraCard.vue'
import AssetZoneOverlay from './AssetZoneOverlay.vue'
import { activity, asset } from './testing'
import type { ProtectedAsset } from '../../api/types'

const DOOR = asset()
const BIKE = asset({
  id: 'bike1',
  name: 'Bike',
  asset_type: 'bicycle',
  enabled: false,
  zone: {
    shape: 'polygon',
    points: [
      [0.7, 0.6],
      [0.9, 0.6],
      [0.8, 0.9],
    ],
  },
})

function mountCard(assets: ProtectedAsset[], extra: Record<string, unknown> = {}) {
  return mount(AssetCameraCard, {
    props: {
      camera: 'Porch',
      assets,
      activity: { 'Front door': activity() },
      snapshotUrl: '/api/assets/snapshot/Porch?v=0',
      perCameraLimit: 12,
      busy: new Set<string>(),
      ...extra,
    },
    attachTo: document.body,
  })
}

describe('AssetCameraCard', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('stays one compact line for a camera with nothing marked', async () => {
    const wrapper = mountCard([])
    expect(wrapper.text()).toContain('Nothing marked on this camera')
    expect(wrapper.find('img').exists()).toBe(false)
    await wrapper.find('button').trigger('click')
    expect(wrapper.emitted('add')).toHaveLength(1)
  })

  it("shows the camera's frame with every zone, and a row for each asset", () => {
    const wrapper = mountCard([DOOR, BIKE])
    expect(wrapper.text()).toContain('1 of 2 watched')
    expect(wrapper.find('img').attributes('src')).toBe('/api/assets/snapshot/Porch?v=0')
    const zones = wrapper.findComponent(AssetZoneOverlay).props('zones')
    expect(zones.map((z) => [z.name, z.muted])).toEqual([
      ['Front door', false],
      ['Bike', true],
    ])
    const rows = wrapper.findAll('.asset-row')
    expect(rows[0].text()).toContain('Front door')
    expect(rows[0].text()).toContain('Door · left of the frame')
    expect(rows[0].text()).toContain('3 clips this week')
    expect(rows[1].text()).toContain('Bike or scooter · lower right of the frame')
    expect(rows[1].text()).toContain('Quiet this week')
    expect(rows[1].text()).toContain('Off')
    expect(rows[1].classes()).toContain('off')
  })

  it('says "clip" for one', () => {
    const wrapper = mountCard([DOOR], { activity: { 'Front door': activity({ clips: 1, top_severity: 'routine' }) } })
    expect(wrapper.text()).toContain('1 clip this week')
  })

  it("shows a severity this build doesn't know as the quietest", () => {
    const newer = activity({ top_severity: 'catastrophic' as never })
    const wrapper = mountCard([DOOR], { activity: { 'Front door': newer } })
    expect(wrapper.find('.asset-activity .p-tag').classes()).toContain('p-tag-secondary')
  })

  it('shows nobody watching as nobody watching', () => {
    const wrapper = mountCard([BIKE])
    expect(wrapper.text()).toContain('0 of 1 watched')
  })

  it('asks to edit, remove, switch off and add', async () => {
    const wrapper = mountCard([DOOR])
    await wrapper.find('[aria-label="Edit Front door"]').trigger('click')
    await wrapper.find('[aria-label="Remove Front door"]').trigger('click')
    wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('Mark another asset'))!
      .trigger('click')
    expect(wrapper.emitted('edit')).toEqual([[DOOR]])
    expect(wrapper.emitted('remove')).toEqual([[DOOR]])
    expect(wrapper.emitted('toggle')).toEqual([[DOOR, false]])
    expect(wrapper.emitted('add')).toHaveLength(1)
  })

  it("holds a switch still while its asset's save is in flight", () => {
    const wrapper = mountCard([DOOR], { busy: new Set(['door1']) })
    expect(wrapper.findComponent(ToggleSwitch).props('disabled')).toBe(true)
  })

  it('lights a zone and its row together', async () => {
    const wrapper = mountCard([DOOR, BIKE])
    const row = wrapper.findAll('.asset-row')[1]
    await row.trigger('mouseenter')
    expect(row.classes()).toContain('highlighted')
    expect(wrapper.findComponent(AssetZoneOverlay).props('highlighted')).toBe('bike1')
    await row.trigger('mouseleave')
    expect(wrapper.findComponent(AssetZoneOverlay).props('highlighted')).toBeNull()

    wrapper.findComponent(AssetZoneOverlay).vm.$emit('hover', 'door1')
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll('.asset-row')[0].classes()).toContain('highlighted')
  })

  it('brings the row into view when its zone is chosen on the frame', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const wrapper = mountCard([DOOR, BIKE])
    wrapper.findComponent(AssetZoneOverlay).vm.$emit('select', 'bike1')
    await wrapper.vm.$nextTick()
    expect(scroll).toHaveBeenCalledWith({ block: 'nearest', behavior: 'smooth' })
    expect(wrapper.findAll('.asset-row')[1].classes()).toContain('highlighted')
  })

  it("says so when the camera's frame can't be shown", async () => {
    const wrapper = mountCard([DOOR])
    await wrapper.find('img').trigger('error')
    expect(wrapper.text()).toContain("isn't available right now")
    expect(wrapper.findComponent(AssetZoneOverlay).exists()).toBe(false)
  })

  it('stops offering another asset at the limit', () => {
    const wrapper = mountCard([DOOR, BIKE], { perCameraLimit: 2 })
    const add = wrapper.findAll('button').find((b) => b.text().includes('Mark another asset'))!
    expect(add.attributes('disabled')).toBeDefined()
    expect(wrapper.text()).toContain('2 is the most one camera can hold')
  })
})
