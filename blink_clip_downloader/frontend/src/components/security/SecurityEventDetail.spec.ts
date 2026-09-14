import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import SecurityEventDetail from './SecurityEventDetail.vue'
import type { SecurityEventRow } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function event(overrides: Partial<SecurityEventRow> = {}): SecurityEventRow {
  return {
    id: 1,
    clip_id: 'c1',
    camera: 'Driveway',
    event_type: 'contact_candidate',
    severity: 'suspicious',
    confidence: 0.72,
    risk_score: 81.4,
    evidence_quality: 0.58,
    detail: 'Possible contact between the person and the blue sedan.',
    subject_label: 'person',
    track_id: 3,
    asset_name: 'blue sedan',
    asset_type: 'vehicle',
    start_offset: 6,
    end_offset: 6,
    evidence: {},
    created_at: '2026-01-05T00:00:00+00:00',
    ...overrides,
  }
}

describe('SecurityEventDetail', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('lists the events with their offsets and confidence', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonResponse({
        events: [event(), event({ id: 2, event_type: 'retreat', start_offset: 10 })],
      }),
    )
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('0:06')
    expect(wrapper.text()).toContain('Contact candidate')
    expect(wrapper.text()).toContain('Retreat')
    expect(wrapper.text()).toContain('72%')
  })

  it('shows the clip-level risk and evidence quality', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ events: [event()] }))
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('81')
    expect(wrapper.text()).toContain('58%')
    expect(wrapper.text()).toContain('moderate')
  })

  it('says the evidence is code-computed and may be wrong', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ events: [event()] }))
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    expect(wrapper.text()).toContain('may disagree')
  })

  it('reports a failure instead of rendering an empty list', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}, false))
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    expect(wrapper.text()).toContain("Could not load this clip's security evidence")
  })

  it('renders nothing but the note when a clip has no events', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ events: [] }))
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    expect(wrapper.findAll('.security-detail-list li')).toHaveLength(0)
  })

  it('reloads when the clip changes', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ events: [event()] }))
    const wrapper = mount(SecurityEventDetail, { props: { clipId: 'c1' } })
    await flushPromises()
    await wrapper.setProps({ clipId: 'c2' })
    await flushPromises()
    expect(vi.mocked(fetch).mock.calls.map((c) => c[0])).toEqual(['/api/security/events/c1', '/api/security/events/c2'])
  })
})
