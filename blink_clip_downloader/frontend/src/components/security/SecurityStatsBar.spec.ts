import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import MeterGroup from 'primevue/metergroup'
import Tag from 'primevue/tag'
import SecurityStatsBar from './SecurityStatsBar.vue'
import type { SecurityStats } from '../../api/types'

const STATS: SecurityStats = {
  by_severity: { critical: 2, suspicious: 5, routine: 11 },
  total: 18,
  days: 7,
}

describe('SecurityStatsBar', () => {
  it('shows every band most severe first, including zeroes', () => {
    // Silently omitting "critical" when the count is zero reads as missing
    // data, not as good news — and a confirmed zero is the point.
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    const tags = wrapper.findAllComponents(Tag)
    expect(tags.map((t) => t.props('value'))).toEqual(['2', '5', '0', '11'])
    expect(wrapper.text()).toContain('Critical')
    expect(wrapper.text()).toContain('Noteworthy')
  })

  it('summarizes the window', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    expect(wrapper.text()).toContain('18 clip(s) with security events in the last 7 days')
  })

  it('shows the distribution as a meter, most severe first', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    const meter = wrapper.findComponent(MeterGroup)
    expect(meter.exists()).toBe(true)
    // Only the bands with something in them — an empty segment is a
    // horizontal line that reads as a broken chart.
    expect(meter.props('value')).toEqual([
      { label: 'Critical', value: 2, color: '#ef4444' },
      { label: 'Suspicious', value: 5, color: '#f59e0b' },
      { label: 'Routine', value: 11, color: '#64748b' },
    ])
    expect(meter.props('max')).toBe(18)
  })

  it('hides the meter when nothing has been recorded', () => {
    const wrapper = mount(SecurityStatsBar, {
      props: { stats: { by_severity: {}, total: 0, days: 7 } },
    })
    expect(wrapper.findComponent(MeterGroup).exists()).toBe(false)
  })

  it('renders a placeholder before stats have loaded', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: null } })
    expect(wrapper.findAllComponents(Tag).map((t) => t.props('value'))).toEqual(['0', '0', '0', '0'])
    expect(wrapper.text()).toContain('—')
  })
})
