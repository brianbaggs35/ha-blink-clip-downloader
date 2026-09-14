import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
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

  it('renders a placeholder before stats have loaded', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: null } })
    expect(wrapper.findAllComponents(Tag).map((t) => t.props('value'))).toEqual(['0', '0', '0', '0'])
    expect(wrapper.text()).toContain('—')
  })
})
