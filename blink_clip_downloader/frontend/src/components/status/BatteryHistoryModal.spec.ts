import { afterEach, describe, expect, it, vi } from 'vitest'
import { DOMWrapper, mount, flushPromises } from '@vue/test-utils'
import BatteryHistoryModal from './BatteryHistoryModal.vue'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

function mountModal(camera = 'Backyard') {
  return mount(BatteryHistoryModal, { props: { camera } })
}

describe('BatteryHistoryModal', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    // Dialog teleports its content to document.body — without clearing it,
    // an unmounted-but-never-explicitly-unmounted previous test's dialog
    // content lingers and pollutes body.text() assertions in later tests.
    document.body.innerHTML = ''
  })

  it('fetches history for the given camera on mount', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(jsonResponse([])))
    vi.stubGlobal('fetch', fetchMock)
    mountModal('Front Door')
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledWith('/api/battery/history/Front%20Door', {})
  })

  it('shows an empty state when there is no recorded history', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('No battery state changes recorded yet')
  })

  it('shows an empty state when the fetch fails, rather than crashing', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('network down'))),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('No battery state changes recorded yet')
  })

  it('labels a low row "Went low" and a recovery row "Back to normal"', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              camera: 'Backyard',
              battery_state: 'low',
              battery_level: 0,
              battery_voltage: 105,
              recorded_at: '2026-01-05T09:00:00Z',
            },
            {
              camera: 'Backyard',
              battery_state: 'ok',
              battery_level: 3,
              battery_voltage: 170,
              recorded_at: '2026-01-01T09:00:00Z',
            },
          ]),
        ),
      ),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Went low')
    expect(body.text()).toContain('Back to normal')
  })

  // One low row at a fixed moment, recovering at three different removes
  // from it -- the only thing under test is which unit the gap is rendered
  // in, so three copies of the same fetch stub only obscured that.
  it.each([
    ['days and hours', '2026-01-03T09:00:00Z', '2d 0h'],
    ['whole hours', '2026-01-01T12:00:00Z', '3h'],
    ['under an hour', '2026-01-01T09:20:00Z', '<1h'],
  ])('formats a recovery %s after going low', async (_label, recoveredAt, expected) => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          // Newest-first, as the endpoint returns it.
          jsonResponse([
            {
              camera: 'Backyard',
              battery_state: 'ok',
              battery_level: 3,
              battery_voltage: 170,
              recorded_at: recoveredAt,
            },
            {
              camera: 'Backyard',
              battery_state: 'low',
              battery_level: 0,
              battery_voltage: 105,
              recorded_at: '2026-01-01T09:00:00Z',
            },
          ]),
        ),
      ),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain(expected)
  })

  it('shows "Ongoing" for a currently-low camera with no recovery yet', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              camera: 'Backyard',
              battery_state: 'low',
              battery_level: 0,
              battery_voltage: 105,
              recorded_at: '2026-01-05T09:00:00Z',
            },
          ]),
        ),
      ),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Ongoing')
  })

  it('shows "—" for a duration that cannot be computed from a malformed timestamp', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() =>
        Promise.resolve(
          jsonResponse([
            {
              camera: 'Backyard',
              battery_state: 'ok',
              battery_level: 3,
              battery_voltage: 170,
              recorded_at: 'not-a-real-timestamp',
            },
            {
              camera: 'Backyard',
              battery_state: 'low',
              battery_level: 0,
              battery_voltage: 105,
              recorded_at: '2026-01-01T09:00:00Z',
            },
          ]),
        ),
      ),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Went low')
    expect(body.text()).toContain('—')
  })

  it('shows a loading indicator before the fetch resolves', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    )
    mountModal()
    await flushPromises()
    const body = new DOMWrapper(document.body)
    expect(body.text()).toContain('Loading')
    expect(body.text()).not.toContain('No battery state changes')
  })

  it('emits close when the dialog is dismissed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mountModal()
    await flushPromises()
    await wrapper.findComponent({ name: 'Dialog' }).vm.$emit('update:visible', false)
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
