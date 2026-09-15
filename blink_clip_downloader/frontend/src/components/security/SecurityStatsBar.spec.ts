import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
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
    expect(wrapper.findAll('.security-stat-count').map((el) => el.text())).toEqual(['2', '5', '0', '11'])
    expect(wrapper.findAll('.security-stat-label').map((el) => el.text())).toEqual([
      'Critical',
      'Suspicious',
      'Noteworthy',
      'Routine',
    ])
  })

  it('plays down a band with nothing in it rather than dressing it as a headline', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    const zeroes = wrapper.findAll('.security-stat').filter((el) => el.classes().includes('is-zero'))
    expect(zeroes).toHaveLength(1)
    expect(zeroes[0].text()).toContain('Noteworthy')
  })

  it('summarizes the window', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    expect(wrapper.text()).toContain('18 clip(s) with security events in the last 7 days')
  })

  it('shows the distribution as a proportion bar, most severe first', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: STATS } })
    const segments = wrapper.findAll('.security-meter-seg')
    // Only the bands with something in them — an empty segment is a
    // hairline that reads as a rendering fault rather than as a zero.
    expect(segments.map((el) => el.attributes('title'))).toEqual([
      'Critical: 2 (11%)',
      'Suspicious: 5 (28%)',
      'Routine: 11 (61%)',
    ])
    // Widths are the counts themselves, so the segments always fill the
    // track regardless of what the total happens to be. (The colours are
    // written as `rgb(R G B)`; jsdom echoes them back comma-separated.)
    expect(segments.map((el) => el.attributes('style'))).toEqual([
      'flex-grow: 2; background: rgb(239, 68, 68);',
      'flex-grow: 5; background: rgb(245, 158, 11);',
      'flex-grow: 11; background: rgb(100, 116, 139);',
    ])
  })

  it('hides the bar when nothing has been recorded', () => {
    const wrapper = mount(SecurityStatsBar, {
      props: { stats: { by_severity: {}, total: 0, days: 7 } },
    })
    expect(wrapper.find('[data-testid="security-meter"]').exists()).toBe(false)
    // ...but the bands themselves stay, so the card still reads as "all
    // four counted, all four zero" rather than as a card that failed.
    expect(wrapper.findAll('.security-stat')).toHaveLength(4)
  })

  it('renders a placeholder before stats have loaded', () => {
    const wrapper = mount(SecurityStatsBar, { props: { stats: null } })
    expect(wrapper.findAll('.security-stat-count').map((el) => el.text())).toEqual(['0', '0', '0', '0'])
    expect(wrapper.text()).toContain('—')
  })
})
