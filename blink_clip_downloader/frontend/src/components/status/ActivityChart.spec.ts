import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ActivityChart from './ActivityChart.vue'

describe('ActivityChart', () => {
  it('shows an empty state when there are no rows', () => {
    const wrapper = mount(ActivityChart, { props: { rows: [] } })
    expect(wrapper.text()).toContain('No recent activity.')
  })

  it('groups rows by date, sums counts, and sorts newest first', () => {
    const wrapper = mount(ActivityChart, {
      props: {
        rows: [
          { date: '2026-01-01', hour: 8, count: 2 },
          { date: '2026-01-01', hour: 9, count: 3 },
          { date: '2026-01-03', hour: 10, count: 1 },
        ],
      },
    })
    const rows = wrapper.findAll('.act-row')
    expect(rows).toHaveLength(2)
    // newest date first
    expect(rows[0].find('.act-count').text()).toBe('1')
    expect(rows[1].find('.act-count').text()).toBe('5')
  })

  it('scales the bar width relative to the busiest day', () => {
    const wrapper = mount(ActivityChart, {
      props: {
        rows: [
          { date: '2026-01-01', hour: 8, count: 10 },
          { date: '2026-01-02', hour: 8, count: 5 },
        ],
      },
    })
    const bars = wrapper.findAll('.act-bar')
    expect(bars[0].attributes('style')).toContain('width: 50')
    expect(bars[1].attributes('style')).toContain('width: 100')
  })

  it('emits select-date with the clicked day', async () => {
    const wrapper = mount(ActivityChart, {
      props: { rows: [{ date: '2026-01-05', hour: 8, count: 1 }] },
    })
    await wrapper.find('.act-bar-wrap').trigger('click')
    expect(wrapper.emitted('select-date')).toEqual([['2026-01-05']])
  })
})
