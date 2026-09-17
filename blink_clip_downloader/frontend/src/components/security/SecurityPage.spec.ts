import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import Button from 'primevue/button'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import SecurityPage from './SecurityPage.vue'
import SecurityEventDetail from './SecurityEventDetail.vue'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import type { SecurityTimelineRow } from '../../api/types'

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: 'x',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function row(overrides: Partial<SecurityTimelineRow> = {}): SecurityTimelineRow {
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
    clip_timestamp: '2026-01-05T02:17:00+00:00',
    file_path: '/clips/c1.mp4',
    starred: false,
    archived: false,
    ai_suspicious: null,
    ai_confidence: null,
    ai_summary: null,
    risk_override_applied: null,
    ...overrides,
  }
}

interface Routes {
  rows?: SecurityTimelineRow[]
  total?: number
  timelineFail?: boolean
  morePage?: SecurityTimelineRow[]
  moreFail?: boolean
  events?: unknown[]
}

let timelineCalls: string[] = []

function routedFetch(routes: Routes) {
  return vi.fn((url: string) => {
    if (url.startsWith('/api/security/timeline')) {
      timelineCalls.push(url)
      if (routes.timelineFail) return Promise.resolve(jsonResponse({}, false))
      if (url.includes('offset=') && !url.includes('offset=0')) {
        if (routes.moreFail) return Promise.resolve(jsonResponse({}, false))
        return Promise.resolve(jsonResponse({ events: routes.morePage ?? [], total: routes.total ?? 0 }))
      }
      return Promise.resolve(
        jsonResponse({ events: routes.rows ?? [], total: routes.total ?? routes.rows?.length ?? 0 }),
      )
    }
    if (url.startsWith('/api/security/stats')) {
      return Promise.resolve(jsonResponse({ by_severity: { suspicious: 1 }, total: 1, days: 7 }))
    }
    if (url.startsWith('/api/security/events/')) {
      return Promise.resolve(jsonResponse({ events: routes.events ?? [] }))
    }
    if (url === '/api/cameras') {
      return Promise.resolve(jsonResponse([{ camera: 'Driveway' }, { camera: 'Back Yard' }]))
    }
    return Promise.resolve(jsonResponse({}))
  })
}

async function mountPage(routes: Routes = {}) {
  vi.stubGlobal('fetch', routedFetch(routes))
  const wrapper = mount(SecurityPage)
  await flushPromises()
  return wrapper
}

describe('SecurityPage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    timelineCalls = []
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders one row per clip with its severity, camera and risk', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    expect(wrapper.find('[data-testid="security-timeline"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Contact candidate')
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Risk 81')
  })

  it('shows the severity summary for the recent window', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    expect(wrapper.find('[data-testid="security-stats"]').text()).toContain('Suspicious')
  })

  it('explains the empty state instead of showing a blank tab', async () => {
    const wrapper = await mountPage({ rows: [] })
    expect(wrapper.text()).toContain('No security events yet')
    expect(wrapper.find('[data-testid="security-timeline"]').exists()).toBe(false)
  })

  it('does not tell a filtering user that nothing has ever been analyzed', async () => {
    // "No security events yet" is a claim about the whole install. Someone
    // who has just picked "Critical only" and got nothing back has not
    // learned that; they have learned their filter is empty.
    const wrapper = await mountPage({ rows: [row()] })
    await wrapper.findAllComponents(Select)[1].setValue('critical')
    timelineCalls = []
    vi.stubGlobal('fetch', routedFetch({ rows: [] }))
    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()
    expect(wrapper.text()).toContain('Nothing matches these filters')
    expect(wrapper.text()).not.toContain('No security events yet')
  })

  it('clears every filter at once from the empty state', async () => {
    const wrapper = await mountPage({ rows: [] })
    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Clear filters')[0]
      .trigger('click')
    await flushPromises()
    // Back to the claim about the whole install, and a request with no
    // filter on it at all.
    expect(wrapper.text()).toContain('No security events yet')
    expect(timelineCalls.at(-1)).not.toContain('camera=')
  })

  it('reports a load failure', async () => {
    const wrapper = await mountPage({ timelineFail: true })
    expect(wrapper.text()).toContain('Failed to load the security timeline')
  })

  it('opens the clip in place rather than making you go and find it', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    const viewer = useClipViewerStore()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => String(b.props('label')).startsWith('View clip'))[0]
      .trigger('click')
    expect(viewer.clipId).toBe('c1')
    expect(viewer.seq).toBe(1)
  })

  it("expands and collapses a clip's full evidence", async () => {
    const wrapper = await mountPage({ rows: [row()] })
    const toggle = () =>
      wrapper.findAllComponents(Button).filter((b) => String(b.props('label')).includes('evidence'))[0]
    await toggle().trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="security-detail"]').exists()).toBe(true)
    await toggle().trigger('click')
    expect(wrapper.find('[data-testid="security-detail"]').exists()).toBe(false)
  })

  it('refetches when a filter changes', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    const selects = wrapper.findAllComponents(Select)
    await selects[1].setValue('critical')
    await flushPromises()
    expect(timelineCalls.at(-1)).toContain('severity=critical')
  })

  it('filters by camera', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()
    expect(timelineCalls.at(-1)).toContain('camera=Back+Yard')
  })

  it('filters by period', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    await wrapper.findComponent(SelectButton).setValue('week')
    await flushPromises()
    expect(timelineCalls.at(-1)).toContain('period=week')
  })

  it('appends the next page without dropping what is already shown', async () => {
    const wrapper = await mountPage({
      rows: [row()],
      total: 2,
      morePage: [row({ id: 2, clip_id: 'c2', camera: 'Back Yard' })],
    })
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.text()).toContain('Back Yard')
  })

  it('keeps what is shown when the next page comes back malformed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('offset=1')) return Promise.resolve(jsonResponse({}))
        if (url.startsWith('/api/security/timeline')) {
          return Promise.resolve(jsonResponse({ events: [row()], total: 2 }))
        }
        if (url.startsWith('/api/security/stats')) {
          return Promise.resolve(jsonResponse({ by_severity: {}, total: 0, days: 7 }))
        }
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Driveway')
    expect(wrapper.findAllComponents(Button).filter((b) => b.props('label') === 'Load more')).toHaveLength(0)
  })

  it('does not repeat a clip when the list grew between pages', async () => {
    // Offset paging over a list that grows at the top: a clip analyzed
    // between the two requests shifts every row down one, so the next page
    // repeats the row that ended the previous one.
    const wrapper = await mountPage({
      rows: [row()],
      total: 3,
      morePage: [row(), row({ id: 2, clip_id: 'c2', camera: 'Back Yard' })],
    })
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.security-row')).toHaveLength(2)
    expect(wrapper.findAll('.security-camera').map((el) => el.text())).toEqual(['Driveway', 'Back Yard'])
  })

  it('hides "load more" once everything is shown', async () => {
    const wrapper = await mountPage({ rows: [row()], total: 1 })
    expect(wrapper.findAllComponents(Button).filter((b) => b.props('label') === 'Load more')).toHaveLength(0)
  })

  it('warns when loading more fails', async () => {
    const wrapper = await mountPage({ rows: [row()], total: 2, moreFail: true })
    const toast = useToastStore()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    expect(toast.message).toBe('Failed to load more events')
    expect(toast.isError).toBe(true)
  })

  it('ignores a slow earlier request that resolves after a newer one', async () => {
    // Changing two filters quickly, or a refresh tick landing mid-request,
    // would otherwise repopulate the tab with rows for a filter the user
    // has already moved off.
    const resolvers: ((value: Response) => void)[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/security/timeline')) {
          return new Promise<Response>((resolve) => resolvers.push(resolve))
        }
        if (url.startsWith('/api/security/stats')) {
          return Promise.resolve(jsonResponse({ by_severity: {}, total: 0, days: 7 }))
        }
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()

    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()
    expect(resolvers).toHaveLength(2)

    // Newest request answers first, then the stale one.
    resolvers[1](jsonResponse({ events: [row({ camera: 'Back Yard' })], total: 1 }))
    await flushPromises()
    resolvers[0](jsonResponse({ events: [row({ camera: 'Stale' })], total: 9 }))
    await flushPromises()

    expect(wrapper.text()).toContain('Back Yard')
    expect(wrapper.text()).not.toContain('Stale')
  })

  it('lets a newer request stand when a slow earlier one fails', async () => {
    // The failure belongs to a filter the user has already moved off —
    // replacing their rows with an error state would be a lie about the
    // filter they are actually looking at.
    const rejecters: ((reason: Error) => void)[] = []
    let call = 0
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.startsWith('/api/security/timeline')) {
          call += 1
          if (call === 1) return new Promise<Response>((_resolve, reject) => rejecters.push(reject))
          return Promise.resolve(jsonResponse({ events: [row({ camera: 'Back Yard' })], total: 1 }))
        }
        if (url.startsWith('/api/security/stats')) {
          return Promise.resolve(jsonResponse({ by_severity: {}, total: 0, days: 7 }))
        }
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()

    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()
    expect(wrapper.text()).toContain('Back Yard')

    rejecters[0](new Error('down'))
    await flushPromises()
    expect(wrapper.text()).toContain('Back Yard')
    expect(wrapper.text()).not.toContain('Could not load security events')
  })

  it('drops a "load more" page fetched under a filter the user has left', async () => {
    const pending: { resolve: (value: Response) => void; reject: (reason: Error) => void }[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url.includes('offset=') && !url.includes('offset=0')) {
          return new Promise<Response>((resolve, reject) => pending.push({ resolve, reject }))
        }
        if (url.startsWith('/api/security/timeline')) {
          return Promise.resolve(jsonResponse({ events: [row()], total: 2 }))
        }
        if (url.startsWith('/api/security/stats')) {
          return Promise.resolve(jsonResponse({ by_severity: {}, total: 0, days: 7 }))
        }
        if (url === '/api/cameras') {
          return Promise.resolve(jsonResponse([{ camera: 'Driveway' }, { camera: 'Back Yard' }]))
        }
        return Promise.resolve(jsonResponse([]))
      }),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()

    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    // The filter moves on while that page is still in flight.
    await wrapper.findAllComponents(Select)[0].setValue('Back Yard')
    await flushPromises()

    pending[0].resolve(jsonResponse({ events: [row({ id: 2, clip_id: 'c2', camera: 'Stale' })], total: 9 }))
    await flushPromises()
    expect(wrapper.text()).not.toContain('Stale')
    // ...and the button it came from is usable again rather than stuck
    // spinning on a request whose answer was thrown away.
    expect(
      wrapper
        .findAllComponents(Button)
        .filter((b) => b.props('label') === 'Load more')[0]
        .props('loading'),
    ).toBe(false)

    // ...and a stale page that fails is equally not the user's problem.
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Load more')[0]
      .trigger('click')
    await flushPromises()
    await wrapper.findAllComponents(Select)[0].setValue(null)
    await flushPromises()
    pending[1].reject(new Error('down'))
    await flushPromises()
    expect(useToastStore().message).toBe('')
  })

  it("shows the model's own verdict beside the code-computed risk", async () => {
    const wrapper = await mountPage({
      rows: [row({ ai_suspicious: false, ai_summary: 'A delivery driver left a parcel.' })],
    })
    expect(wrapper.text()).toContain('AI: nothing unusual')
    expect(wrapper.text()).toContain('A delivery driver left a parcel.')
  })

  it('marks a clip the model itself called suspicious', async () => {
    const wrapper = await mountPage({ rows: [row({ severity: 'suspicious', ai_suspicious: true })] })
    expect(wrapper.text()).toContain('AI: suspicious')
    expect(wrapper.text()).not.toContain('disagreement')
  })

  it('points at a clip where code and model reached opposite conclusions', async () => {
    // A clip the geometry rates highly and the model waved through is the
    // one a person most needs to look at themselves.
    const wrapper = await mountPage({ rows: [row({ severity: 'critical', ai_suspicious: false })] })
    expect(wrapper.text()).toContain('disagreement')
  })

  it('does not call a risk override a disagreement', async () => {
    const wrapper = await mountPage({
      rows: [row({ severity: 'critical', ai_suspicious: true, risk_override_applied: true })],
    })
    expect(wrapper.text()).toContain('Flagged on evidence')
    expect(wrapper.text()).not.toContain('disagreement')
  })

  it('says nothing about a verdict for a clip that has no analysis row', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    expect(wrapper.text()).not.toContain('AI:')
    expect(wrapper.text()).not.toContain('disagreement')
  })

  it('opens the clip at the second the event was measured at', async () => {
    const wrapper = await mountPage({ rows: [row({ start_offset: 6 })] })
    const viewer = useClipViewerStore()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => String(b.props('label')).startsWith('View clip'))[0]
      .trigger('click')
    expect(viewer.clipId).toBe('c1')
    expect(viewer.startAt).toBe(6)
  })

  it('opens from the top when the event is at the very start', async () => {
    const wrapper = await mountPage({ rows: [row({ start_offset: 0 })] })
    const viewer = useClipViewerStore()
    const button = wrapper.findAllComponents(Button).filter((b) => String(b.props('label')).startsWith('View clip'))[0]
    expect(button.props('label')).toBe('View clip')
    await button.trigger('click')
    expect(viewer.startAt).toBe(0)
  })

  it('seeks the clip when an event time in the evidence panel is clicked', async () => {
    const wrapper = await mountPage({ rows: [row()], events: [] })
    const viewer = useClipViewerStore()
    await wrapper
      .findAllComponents(Button)
      .filter((b) => b.props('label') === 'Show evidence')[0]
      .trigger('click')
    await flushPromises()
    wrapper.findComponent(SecurityEventDetail).vm.$emit('seek', 12)
    expect(viewer.clipId).toBe('c1')
    expect(viewer.startAt).toBe(12)
  })

  it('opens the clip from the top when its thumbnail is clicked', async () => {
    const wrapper = await mountPage({ rows: [row({ start_offset: 6 })] })
    const viewer = useClipViewerStore()
    await wrapper.find('.security-thumb').trigger('click')
    expect(viewer.clipId).toBe('c1')
    expect(viewer.startAt).toBeNull()
  })

  it('drops the thumbnail rather than showing a broken image', async () => {
    const wrapper = await mountPage({ rows: [row()] })
    await wrapper.find('.security-thumb img').trigger('error')
    expect(wrapper.find('.security-thumb').exists()).toBe(false)
  })

  it('reloads on the global refresh tick', async () => {
    await mountPage({ rows: [row()] })
    const before = timelineCalls.length
    useRefreshStore().bump()
    await flushPromises()
    expect(timelineCalls.length).toBeGreaterThan(before)
  })

  it('keeps the timeline on screen while a background refresh is in flight', async () => {
    // The refresh signal fires from other tabs' actions — including the clip
    // modal this timeline itself opens — so a tick must not replace rows the
    // user is reading (and their expanded evidence) with a spinner.
    const wrapper = await mountPage({ rows: [row()] })
    useRefreshStore().bump()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('[data-testid="security-timeline"]').exists()).toBe(true)
    expect(wrapper.findComponent(LoadingIndicator).exists()).toBe(false)
    await flushPromises()
    expect(wrapper.find('[data-testid="security-timeline"]').exists()).toBe(true)
  })

  it('survives the camera list failing to load', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((url: string) => {
        if (url === '/api/cameras') return Promise.resolve(jsonResponse({}, false))
        if (url.startsWith('/api/security/stats')) {
          return Promise.resolve(jsonResponse({ by_severity: {}, total: 0, days: 7 }))
        }
        return Promise.resolve(jsonResponse({ events: [], total: 0 }))
      }),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()
    expect(wrapper.findAllComponents(Select)[0].props('options')).toEqual([{ label: 'All cameras', value: null }])
  })

  it('shows the empty state when the response has no events array', async () => {
    // A render error here would blank the whole tab rather than degrade.
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse({}))),
    )
    const wrapper = mount(SecurityPage)
    await flushPromises()
    expect(wrapper.text()).toContain('No security events yet')
  })

  it('falls back to the raw string for an unparseable timestamp', async () => {
    const wrapper = await mountPage({ rows: [row({ clip_timestamp: 'not-a-date' })] })
    expect(wrapper.find('.security-when').text()).toBe('not-a-date')
    // ...and it heads its own day rather than rendering "Invalid Date".
    expect(wrapper.find('.security-day-head').text()).toContain('not-a-date')
  })

  it('files rows under the day they were recorded', async () => {
    // The date used to be repeated on every single row, in a fixed column
    // that took half the width of the tab with it.
    // Anchored to local noon, NOT to `new Date()` minus an offset: the
    // component groups by local calendar day, so a test built from
    // "now - 1 hour" files that row under *yesterday* whenever the suite
    // runs in the midnight hour, and this test then reports Today = 1.
    // CI caught exactly that at 00:28. Noon is comfortably clear of both
    // ends of the day at every hour the suite might run, and of a DST
    // shift either way.
    const noonToday = new Date()
    noonToday.setHours(12, 0, 0, 0)
    const noonYesterday = new Date(noonToday)
    noonYesterday.setDate(noonYesterday.getDate() - 1)
    const wrapper = await mountPage({
      rows: [
        row({ clip_id: 'a', clip_timestamp: noonToday.toISOString() }),
        row({ clip_id: 'b', clip_timestamp: new Date(noonToday.getTime() - 3_600_000).toISOString() }),
        row({ clip_id: 'c', clip_timestamp: noonYesterday.toISOString() }),
      ],
    })
    const headings = wrapper.findAll('.security-day-head')
    expect(headings).toHaveLength(2)
    expect(headings[0].text()).toContain('Today')
    // The count beside the heading, so a busy day says so before it is read.
    expect(headings[0].find('.security-day-count').text()).toBe('2')
    expect(headings[1].text()).toContain('Yesterday')
    expect(wrapper.findAll('.security-day')[1].findAll('.security-row')).toHaveLength(1)
  })

  it('names an older day outright rather than counting back to it', async () => {
    const wrapper = await mountPage({ rows: [row({ clip_timestamp: '2026-01-05T02:17:00Z' })] })
    const heading = wrapper.find('.security-day-head').text()
    expect(heading).not.toContain('Today')
    expect(heading).toContain('Jan')
  })

  it('marks the second an event was measured at on its own thumbnail', async () => {
    const wrapper = await mountPage({ rows: [row({ start_offset: 18 })] })
    expect(wrapper.find('.security-thumb-at').text()).toBe('0:18')
  })

  it('has nothing to mark on the thumbnail for an event at the very start', async () => {
    const wrapper = await mountPage({ rows: [row({ start_offset: 0 })] })
    expect(wrapper.find('.security-thumb-at').exists()).toBe(false)
  })

  it('says how much of the timeline is on screen', async () => {
    const wrapper = await mountPage({ rows: [row()], total: 9 })
    expect(wrapper.find('.security-toolbar-count').text()).toBe('Showing 1 of 9')
  })
})
