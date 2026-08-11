import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import BatteryStatusStrip from './BatteryStatusStrip.vue'
import type { BatteryStatus } from '../../api/types'

const READINGS: BatteryStatus[] = [
  {
    camera: 'Front Door',
    battery_state: 'ok',
    battery_level: 3,
    battery_voltage: 165,
    recorded_at: '2026-01-05T08:00:00Z',
  },
  {
    camera: 'Backyard',
    battery_state: 'low',
    battery_level: 0,
    battery_voltage: 105,
    recorded_at: '2026-01-05T09:00:00Z',
  },
  {
    camera: 'Garage',
    battery_state: 'ok',
    battery_level: 3,
    battery_voltage: null,
    recorded_at: '2026-01-05T09:00:00Z',
  },
]

describe('BatteryStatusStrip', () => {
  it('renders one tile per reading', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    expect(wrapper.findAll('.battery-tile')).toHaveLength(3)
  })

  it('marks a low reading with the low class and label', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    const tiles = wrapper.findAll('.battery-tile')
    const back = tiles.find((t) => t.text().includes('Backyard'))!
    expect(back.classes()).toContain('low')
    expect(back.text()).toContain('Low')
  })

  it('does not mark a normal reading as low', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    const tiles = wrapper.findAll('.battery-tile')
    const front = tiles.find((t) => t.text().includes('Front Door'))!
    expect(front.classes()).not.toContain('low')
    expect(front.text()).toContain('Normal')
  })

  it('shows voltage as supplementary text when present', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    const front = wrapper.findAll('.battery-tile').find((t) => t.text().includes('Front Door'))!
    expect(front.text()).toContain('1.65V')
  })

  it('never shows battery_level as a fabricated percentage', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    expect(wrapper.text()).not.toContain('%')
  })

  it('omits voltage text when not reported', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    const garage = wrapper.findAll('.battery-tile').find((t) => t.text().includes('Garage'))!
    expect(garage.text()).not.toContain('·')
  })

  it('emits select-camera with the clicked camera name', async () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: READINGS } })
    const back = wrapper.findAll('.battery-tile').find((t) => t.text().includes('Backyard'))!
    await back.trigger('click')
    expect(wrapper.emitted('select-camera')).toEqual([['Backyard']])
  })

  it('renders nothing when there are no readings', () => {
    const wrapper = mount(BatteryStatusStrip, { props: { readings: [] } })
    expect(wrapper.findAll('.battery-tile')).toHaveLength(0)
  })
})
