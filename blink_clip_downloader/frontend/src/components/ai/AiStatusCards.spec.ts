import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AiStatusCards from './AiStatusCards.vue'
import type { AiStatus } from '../../api/types'

function baseStatus(overrides: Partial<AiStatus> = {}): AiStatus {
  return {
    enabled: true,
    prompt_debug_enabled: false,
    smtp_configured: false,
    analysis_stats: {
      total_analyzed: 12,
      suspicious_count: 2,
      total_frames_analyzed: 0,
      frames_analyzed_today: 0,
      last_analysis: '2026-01-05T10:00:00Z',
    },
    ...overrides,
  }
}

describe('AiStatusCards', () => {
  it('shows "Always active" when the queue reports no schedule window', () => {
    const wrapper = mount(AiStatusCards, {
      props: {
        status: baseStatus({
          queue: {
            pending: 0,
            processing: 0,
            completed: 0,
            failed: 0,
            in_schedule: true,
            min_confidence: 0.5,
            schedule_start: null,
            schedule_end: null,
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('Always active')
  })

  it('shows a loading placeholder when no queue data is present yet', () => {
    const wrapper = mount(AiStatusCards, { props: { status: baseStatus() } })
    expect(wrapper.text()).toContain('Loading')
  })

  it('shows the schedule window and active/waiting state', () => {
    const wrapper = mount(AiStatusCards, {
      props: {
        status: baseStatus({
          queue: {
            pending: 3,
            processing: 1,
            completed: 5,
            failed: 0,
            in_schedule: true,
            min_confidence: 0.5,
            schedule_start: '08:00',
            schedule_end: '20:00',
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('08:00 – 20:00')
    expect(wrapper.text()).toContain('🟢 Active')
  })

  it('shows Waiting when outside the schedule window', () => {
    const wrapper = mount(AiStatusCards, {
      props: {
        status: baseStatus({
          queue: {
            pending: 0,
            processing: 0,
            completed: 0,
            failed: 0,
            in_schedule: false,
            min_confidence: 0.5,
            schedule_start: '08:00',
            schedule_end: '20:00',
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('🔴 Waiting')
  })

  it('shows queue counts', () => {
    const wrapper = mount(AiStatusCards, {
      props: {
        status: baseStatus({
          queue: {
            pending: 3,
            processing: 1,
            completed: 5,
            failed: 2,
            in_schedule: true,
            min_confidence: 0.5,
            schedule_start: null,
            schedule_end: null,
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('3')
    expect(wrapper.text()).toContain('1')
    expect(wrapper.text()).toContain('5')
    expect(wrapper.text()).toContain('2')
  })

  it('shows analysis stats and formats last-analyzed time', () => {
    const wrapper = mount(AiStatusCards, { props: { status: baseStatus() } })
    expect(wrapper.text()).toContain('12')
    expect(wrapper.text()).toContain('2')
    expect(wrapper.text()).toContain(new Date('2026-01-05T10:00:00Z').toLocaleString())
  })

  it('shows an em dash when nothing has been analyzed yet', () => {
    const wrapper = mount(AiStatusCards, {
      props: {
        status: baseStatus({
          analysis_stats: {
            total_analyzed: 0,
            suspicious_count: 0,
            total_frames_analyzed: 0,
            frames_analyzed_today: 0,
            last_analysis: null,
          },
        }),
      },
    })
    expect(wrapper.text()).toContain('—')
  })
})
