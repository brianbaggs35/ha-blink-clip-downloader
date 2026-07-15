import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import VehicleZonePicker from './VehicleZonePicker.vue'
import type { CarZone, ClipListItem } from '../../api/types'

function makeClip(id: string): ClipListItem {
  return {
    id,
    camera: 'Driveway',
    file_path: `/data/${id}.mp4`,
    timestamp: '2026-01-05T10:00:00Z',
    size_bytes: 1000,
    duration: 5,
    source: 'pir',
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '2026-01-05T10:01:00Z',
    archived: false,
    archive_path: '',
    notified: false,
    face_recognized: false,
  }
}

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const CONTAINER_SIZE = { width: 400, height: 300 }

describe('VehicleZonePicker', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([makeClip('c1'), makeClip('c2')]))),
    )
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      width: CONTAINER_SIZE.width,
      height: CONTAINER_SIZE.height,
      top: 0,
      left: 0,
      right: CONTAINER_SIZE.width,
      bottom: CONTAINER_SIZE.height,
      x: 0,
      y: 0,
      toJSON: () => '',
    })
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  async function mountPicker(modelValue: CarZone | null = null) {
    const wrapper = mount(VehicleZonePicker, { props: { camera: 'Driveway', modelValue } })
    await flushPromises()
    await wrapper.find('img.picker-image').trigger('load')
    await flushPromises()
    return wrapper
  }

  // VTU's trigger() tries to reassign event properties after construction,
  // which throws for getter-only properties like clientX/clientY that
  // PointerEvent inherits from MouseEvent.prototype (a VTU/jsdom prototype-
  // chain quirk) — dispatching a real, fully-constructed PointerEvent
  // sidesteps that entirely and is closer to what a real browser sends.
  async function firePointer(el: Element, type: string, clientX: number, clientY: number) {
    el.dispatchEvent(new PointerEvent(type, { clientX, clientY, bubbles: true }))
    await flushPromises()
  }

  it('loads and renders a thumbnail strip of recent clips for the camera', async () => {
    const wrapper = await mountPicker()
    expect(fetch).toHaveBeenCalledWith('/api/clips?camera=Driveway&sort=newest&limit=8', {})
    expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(2)
  })

  it('shows a message when the camera has no clips yet', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(jsonResponse([]))),
    )
    const wrapper = mount(VehicleZonePicker, { props: { camera: 'Driveway', modelValue: null } })
    await flushPromises()
    expect(wrapper.text()).toContain('download a clip first')
  })

  it('shows an error message when loading clips fails', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('down'))),
    )
    const wrapper = mount(VehicleZonePicker, { props: { camera: 'Driveway', modelValue: null } })
    await flushPromises()
    expect(wrapper.text()).toContain('Failed to load recent clips')
  })

  it('draws a new zone via pointer drag and emits the fraction rectangle', async () => {
    const wrapper = await mountPicker()
    const overlay = wrapper.find('.picker-overlay')
    await firePointer(overlay.element, 'pointerdown', 40, 30)
    await firePointer(overlay.element, 'pointermove', 200, 150)
    await firePointer(overlay.element, 'pointerup', 200, 150)

    const emitted = wrapper.emitted('update:modelValue')
    expect(emitted).toBeTruthy()
    const last = emitted![emitted!.length - 1][0]
    expect(last).toEqual({ x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 })
  })

  it('does not emit a zone for a click without a meaningful drag', async () => {
    const wrapper = await mountPicker()
    const overlay = wrapper.find('.picker-overlay')
    await firePointer(overlay.element, 'pointerdown', 40, 30)
    await firePointer(overlay.element, 'pointerup', 41, 31)

    const emitted = wrapper.emitted('update:modelValue')
    expect(emitted![emitted!.length - 1][0]).toBeNull()
  })

  it('renders an existing zone from modelValue and lets it be cleared', async () => {
    const wrapper = await mountPicker({ x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 })
    expect(wrapper.find('.zone-rect').exists()).toBe(true)

    const clearBtn = wrapper.findAll('button').find((b) => b.text().includes('Clear zone'))!
    await clearBtn.trigger('click')

    expect(wrapper.find('.zone-rect').exists()).toBe(false)
    const emitted = wrapper.emitted('update:modelValue')
    expect(emitted![emitted!.length - 1][0]).toBeNull()
  })

  it('re-renders the zone when modelValue changes externally (e.g. switching cameras)', async () => {
    const wrapper = await mountPicker(null)
    expect(wrapper.find('.zone-rect').exists()).toBe(false)

    await wrapper.setProps({ modelValue: { x_min: 0.2, y_min: 0.2, x_max: 0.6, y_max: 0.6 } })
    await flushPromises()
    expect(wrapper.find('.zone-rect').exists()).toBe(true)

    await wrapper.setProps({ modelValue: null })
    await flushPromises()
    expect(wrapper.find('.zone-rect').exists()).toBe(false)
  })

  it('resizes an existing zone by dragging a corner handle', async () => {
    const wrapper = await mountPicker({ x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 })
    // Existing rect in pixels: x=40,y=30,width=160,height=120 -> se corner at (200,150)
    const overlay = wrapper.find('.picker-overlay')
    await firePointer(overlay.element, 'pointerdown', 200, 150)
    await firePointer(overlay.element, 'pointermove', 400, 300)
    await firePointer(overlay.element, 'pointerup', 400, 300)

    const emitted = wrapper.emitted('update:modelValue')
    const last = emitted![emitted!.length - 1][0]
    expect(last).toEqual({ x_min: 0.1, y_min: 0.1, x_max: 1, y_max: 1 })
  })

  it('moves an existing zone by dragging its body', async () => {
    const wrapper = await mountPicker({ x_min: 0.1, y_min: 0.1, x_max: 0.3, y_max: 0.3 })
    // Existing rect in pixels: x=40,y=30,width=80,height=60 -> body center ~ (80,60)
    const overlay = wrapper.find('.picker-overlay')
    await firePointer(overlay.element, 'pointerdown', 80, 60)
    await firePointer(overlay.element, 'pointermove', 120, 90)
    await firePointer(overlay.element, 'pointerup', 120, 90)

    const emitted = wrapper.emitted('update:modelValue')
    const last = emitted![emitted!.length - 1][0]
    expect(last).toEqual({ x_min: 0.2, y_min: 0.2, x_max: 0.4, y_max: 0.4 })
  })

  it('switches the displayed frame when a different thumbnail is clicked', async () => {
    const wrapper = await mountPicker()
    const items = wrapper.findAll('.thumb-strip-item')
    await items[1].trigger('click')
    expect(items[1].classes()).toContain('active')
  })
})
