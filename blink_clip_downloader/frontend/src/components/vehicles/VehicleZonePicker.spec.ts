import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import PrimeVue from 'primevue/config'
import SelectButton from 'primevue/selectbutton'
import VehicleZonePicker from './VehicleZonePicker.vue'
import { useConfirmStore } from '../../stores/confirm'
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

function jsonResponse(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    statusText: ok ? 'OK' : 'Error',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(''),
  } as Response
}

const CONTAINER_SIZE = { width: 400, height: 300 }
const RECT_ZONE: CarZone = { shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 }
const POLYGON_ZONE: CarZone = {
  shape: 'polygon',
  points: [
    [0.1, 0.1],
    [0.5, 0.1],
    [0.3, 0.5],
  ],
}

function routedFetch(extra: (url: string, init?: RequestInit) => Promise<Response> | undefined) {
  return vi.fn((url: string, init?: RequestInit) => {
    const result = extra(url, init)
    if (result) return result
    if (url.includes('/api/clips')) return Promise.resolve(jsonResponse([makeClip('c1'), makeClip('c2')]))
    return Promise.reject(new Error(`unhandled fetch: ${url}`))
  })
}

describe('VehicleZonePicker', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.stubGlobal(
      'fetch',
      routedFetch(() => undefined),
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

  function mountPicker(modelValue: CarZone | null = null) {
    return mount(VehicleZonePicker, {
      props: { camera: 'Driveway', modelValue },
      global: { plugins: [PrimeVue] },
    })
  }

  async function mountInEditMode() {
    const wrapper = mountPicker(null)
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

  async function switchToFreeform(wrapper: ReturnType<typeof mount>) {
    await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'polygon')
  }

  describe('preview mode (zone already saved)', () => {
    it('shows the saved snapshot with a rectangle overlay, without fetching recent clips', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()

      expect(wrapper.find('.zone-rect').exists()).toBe(true)
      expect(wrapper.text()).toContain('Edit zone')
      expect(wrapper.text()).toContain('Clear zone')
      expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/api/clips'))).toBe(false)
    })

    it('shows the saved snapshot with a polygon overlay', async () => {
      const wrapper = mountPicker(POLYGON_ZONE)
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()

      expect(wrapper.find('.zone-polygon').exists()).toBe(true)
      expect(wrapper.find('.zone-rect').exists()).toBe(false)
    })

    it('switches to edit mode and loads recent clips when "Edit zone" is clicked', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      const editBtn = wrapper.findAll('button').find((b) => b.text().includes('Edit zone'))!
      await editBtn.trigger('click')
      await flushPromises()

      expect(wrapper.text()).toContain('Save zone')
      expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('/api/clips'))).toBe(true)
      // Editing an existing zone is a fresh redraw, not a resume — the
      // empty-state message still shouldn't show since a zone *does* exist.
      expect(wrapper.text()).not.toContain('No vehicle selected')
    })

    it('clears the zone and returns to the empty edit state on confirm', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'DELETE') {
            return Promise.resolve(jsonResponse({ saved: true }))
          }
          return undefined
        }),
      )
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      const confirm = useConfirmStore()
      const clearBtn = wrapper.findAll('button').find((b) => b.text().includes('Clear zone'))!
      const clickPromise = clearBtn.trigger('click')
      await flushPromises()
      confirm.settle(true)
      await clickPromise
      await flushPromises()

      expect(fetch).toHaveBeenCalledWith('/api/vehicle/zone/Driveway', { method: 'DELETE' })
      expect(wrapper.emitted('update:modelValue')![0][0]).toBeNull()
      expect(wrapper.text()).toContain('No vehicle selected')
    })

    it('does not clear the zone when the confirmation is declined', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()
      const confirm = useConfirmStore()
      const clearBtn = wrapper.findAll('button').find((b) => b.text().includes('Clear zone'))!
      const clickPromise = clearBtn.trigger('click')
      await flushPromises()
      confirm.settle(false)
      await clickPromise
      await flushPromises()

      expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'DELETE')).toBe(false)
      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      expect(wrapper.find('.zone-rect').exists()).toBe(true)
    })

    it('does not crash and leaves the zone in place when clearing fails', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'DELETE') {
            return Promise.reject(new Error('down'))
          }
          return undefined
        }),
      )
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      const confirm = useConfirmStore()
      const clearBtn = wrapper.findAll('button').find((b) => b.text().includes('Clear zone'))!
      const clickPromise = clearBtn.trigger('click')
      await flushPromises()
      confirm.settle(true)
      await clickPromise
      await flushPromises()

      expect(wrapper.exists()).toBe(true)
      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
    })
  })

  describe('edit mode (no zone yet)', () => {
    it('shows the empty-state message', async () => {
      const wrapper = await mountInEditMode()
      expect(wrapper.text()).toContain('No vehicle selected')
      expect(wrapper.text()).toContain('click save to set a vehicle')
    })

    it('shows a message when the camera has no clips yet', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch(() => Promise.resolve(jsonResponse([]))),
      )
      const wrapper = mountPicker(null)
      await flushPromises()
      expect(wrapper.text()).toContain('download a clip first')
    })

    it('shows an error message when loading clips fails', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn(() => Promise.reject(new Error('down'))),
      )
      const wrapper = mountPicker(null)
      await flushPromises()
      expect(wrapper.text()).toContain('Failed to load recent clips')
    })

    it('switches the displayed frame when a different thumbnail is clicked', async () => {
      const wrapper = await mountInEditMode()
      const items = wrapper.findAll('.thumb-strip-item')
      await items[1].trigger('click')
      expect(items[1].classes()).toContain('active')
    })

    it('draws a rectangle draft but does not save or emit until "Save zone" is clicked', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)

      expect(wrapper.find('.zone-rect').exists()).toBe(true)
      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'PUT')).toBe(false)
      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeUndefined()
    })

    it('keeps "Save zone" disabled for a click without a meaningful drag', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointerup', 41, 31)

      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeDefined()
    })

    it('resizes the in-progress rectangle via a corner handle before saving', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)
      // se corner of the just-drawn rect sits at (200, 150) in this container.
      await firePointer(overlay.element, 'pointerdown', 200, 150)
      await firePointer(overlay.element, 'pointermove', 400, 300)
      await firePointer(overlay.element, 'pointerup', 400, 300)

      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'PUT') {
            const body = JSON.parse(String(init.body))
            return Promise.resolve(jsonResponse({ saved: true, car_zone: body.zone }))
          }
          return undefined
        }),
      )
      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      await saveBtn.trigger('click')
      await flushPromises()

      const [, init] = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/api/vehicle/zone/'))!
      const body = JSON.parse(String((init as RequestInit).body))
      expect(body.zone).toEqual({ shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 1, y_max: 1 })
    })

    it('saves a rectangle zone: PUTs the zone + selected clip id, emits the server result, and returns to preview', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'PUT') {
            return Promise.resolve(jsonResponse({ saved: true, car_zone: RECT_ZONE }))
          }
          return undefined
        }),
      )
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)

      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      await saveBtn.trigger('click')
      await flushPromises()

      expect(fetch).toHaveBeenCalledWith(
        '/api/vehicle/zone/Driveway',
        expect.objectContaining({
          method: 'PUT',
          body: JSON.stringify({
            zone: { shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.5 },
            clip_id: 'c1',
          }),
        }),
      )
      expect(wrapper.emitted('update:modelValue')![0][0]).toEqual(RECT_ZONE)
      expect(wrapper.find('.thumb-strip').exists()).toBe(false)
      expect(wrapper.text()).toContain('Edit zone')
    })

    it('does not save or crash when the save request fails, and keeps the draft', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'PUT') {
            return Promise.reject(new Error('down'))
          }
          return undefined
        }),
      )
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)

      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      await saveBtn.trigger('click')
      await flushPromises()

      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      expect(wrapper.find('.zone-rect').exists()).toBe(true)
    })

    it('clears the draft when switching thumbnails mid-draw', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)
      expect(wrapper.find('.zone-rect').exists()).toBe(true)

      const items = wrapper.findAll('.thumb-strip-item')
      await items[1].trigger('click')

      expect(wrapper.find('.zone-rect').exists()).toBe(false)
    })
  })

  describe('freeform (polygon) drawing', () => {
    it('traces a path and only enables "Save zone" once it has at least 3 points', async () => {
      const wrapper = await mountInEditMode()
      await switchToFreeform(wrapper)
      const overlay = wrapper.find('.picker-overlay')

      await firePointer(overlay.element, 'pointerdown', 40, 30)
      let saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeDefined()

      await firePointer(overlay.element, 'pointermove', 200, 30)
      await firePointer(overlay.element, 'pointermove', 120, 150)
      await firePointer(overlay.element, 'pointerup', 120, 150)

      expect(wrapper.find('.zone-polygon').exists()).toBe(true)
      saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeUndefined()
    })

    it('saves a polygon zone with fractional points', async () => {
      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'PUT') {
            return Promise.resolve(jsonResponse({ saved: true, car_zone: POLYGON_ZONE }))
          }
          return undefined
        }),
      )
      const wrapper = await mountInEditMode()
      await switchToFreeform(wrapper)
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 30)
      await firePointer(overlay.element, 'pointermove', 120, 150)
      await firePointer(overlay.element, 'pointerup', 120, 150)

      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      await saveBtn.trigger('click')
      await flushPromises()

      const [, init] = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/api/vehicle/zone/'))!
      const body = JSON.parse(String((init as RequestInit).body))
      expect(body.zone.shape).toBe('polygon')
      expect(body.zone.points).toEqual([
        [0.1, 0.1],
        [0.5, 0.1],
        [0.3, 0.5],
      ])
      expect(wrapper.emitted('update:modelValue')![0][0]).toEqual(POLYGON_ZONE)
    })

    it('clears the draft when switching from freeform back to rectangle', async () => {
      const wrapper = await mountInEditMode()
      await switchToFreeform(wrapper)
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 30)
      await firePointer(overlay.element, 'pointermove', 120, 150)
      await firePointer(overlay.element, 'pointerup', 120, 150)
      expect(wrapper.find('.zone-polygon').exists()).toBe(true)

      await wrapper.findComponent(SelectButton).vm.$emit('update:modelValue', 'rect')

      expect(wrapper.find('.zone-polygon').exists()).toBe(false)
      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeDefined()
    })
  })

  describe('clear draft', () => {
    it('does not show a "Clear" button when there is no draft yet', async () => {
      const wrapper = await mountInEditMode()
      expect(wrapper.findAll('button').some((b) => b.text() === 'Clear')).toBe(false)
    })

    it('wipes an in-progress rectangle draft without touching the saved zone or clip selection', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)
      expect(wrapper.find('.zone-rect').exists()).toBe(true)

      const clearBtn = wrapper.findAll('button').find((b) => b.text() === 'Clear')!
      await clearBtn.trigger('click')

      expect(wrapper.find('.zone-rect').exists()).toBe(false)
      expect(wrapper.findAll('button').some((b) => b.text() === 'Clear')).toBe(false)
      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      expect(saveBtn.attributes('disabled')).toBeDefined()
      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      // Still on the same frame/shape — redrawing immediately is possible
      // without re-selecting a thumbnail or the shape toggle.
      expect(wrapper.find('.thumb-strip-item.active').exists()).toBe(true)
    })

    it('wipes an in-progress freeform draft and lets the user retrace immediately', async () => {
      const wrapper = await mountInEditMode()
      await switchToFreeform(wrapper)
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 30)
      await firePointer(overlay.element, 'pointermove', 120, 150)
      await firePointer(overlay.element, 'pointerup', 120, 150)
      expect(wrapper.find('.zone-polygon').exists()).toBe(true)

      const clearBtn = wrapper.findAll('button').find((b) => b.text() === 'Clear')!
      await clearBtn.trigger('click')
      expect(wrapper.find('.zone-polygon').exists()).toBe(false)

      // Retrace on the same (still-freeform) canvas without switching shape.
      await firePointer(overlay.element, 'pointerdown', 10, 10)
      await firePointer(overlay.element, 'pointermove', 90, 10)
      await firePointer(overlay.element, 'pointermove', 50, 90)
      await firePointer(overlay.element, 'pointerup', 50, 90)
      expect(wrapper.find('.zone-polygon').exists()).toBe(true)
    })

    it('leaves an already-saved zone alone when clearing a fresh redraw draft', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      const editBtn = wrapper.findAll('button').find((b) => b.text().includes('Edit zone'))!
      await editBtn.trigger('click')
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()

      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 10, 10)
      await firePointer(overlay.element, 'pointermove', 100, 100)
      await firePointer(overlay.element, 'pointerup', 100, 100)

      const clearBtn = wrapper.findAll('button').find((b) => b.text() === 'Clear')!
      await clearBtn.trigger('click')

      expect(wrapper.find('.zone-rect').exists()).toBe(false)
      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      // "Cancel" (back to the old saved zone) is still available too.
      expect(wrapper.findAll('button').some((b) => b.text() === 'Cancel')).toBe(true)
    })
  })

  describe('cancel', () => {
    it('discards the draft and returns to preview without saving', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      const editBtn = wrapper.findAll('button').find((b) => b.text().includes('Edit zone'))!
      await editBtn.trigger('click')
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()

      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 10, 10)
      await firePointer(overlay.element, 'pointermove', 100, 100)
      await firePointer(overlay.element, 'pointerup', 100, 100)

      const cancelBtn = wrapper.findAll('button').find((b) => b.text() === 'Cancel')!
      await cancelBtn.trigger('click')
      await flushPromises()

      expect(wrapper.emitted('update:modelValue')).toBeFalsy()
      expect(wrapper.text()).toContain('Edit zone')
      expect(vi.mocked(fetch).mock.calls.some(([, init]) => (init as RequestInit)?.method === 'PUT')).toBe(false)
    })
  })

  describe('moving an in-progress rectangle', () => {
    it('drags the rectangle body (not a corner handle) to translate it', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)

      // (100, 90) sits well inside the drawn rect's body, away from any corner.
      await firePointer(overlay.element, 'pointerdown', 100, 90)
      await firePointer(overlay.element, 'pointermove', 120, 100)
      await firePointer(overlay.element, 'pointerup', 120, 100)

      vi.stubGlobal(
        'fetch',
        routedFetch((url, init) => {
          if (url.includes('/api/vehicle/zone/Driveway') && init?.method === 'PUT') {
            const body = JSON.parse(String(init.body))
            return Promise.resolve(jsonResponse({ saved: true, car_zone: body.zone }))
          }
          return undefined
        }),
      )
      const saveBtn = wrapper.findAll('button').find((b) => b.text().includes('Save zone'))!
      await saveBtn.trigger('click')
      await flushPromises()

      const [, init] = vi.mocked(fetch).mock.calls.find(([url]) => String(url).includes('/api/vehicle/zone/'))!
      const body = JSON.parse(String((init as RequestInit).body))
      // Original rect was x=40,y=30,w=160,h=120; translated by (20, 10).
      expect(body.zone).toEqual({
        shape: 'rect',
        x_min: 0.15,
        y_min: 0.13333333333333333,
        x_max: 0.55,
        y_max: 0.5333333333333333,
      })
    })
  })

  describe('pointer edge cases', () => {
    it('ignores a pointermove with no active drag or freeform trace', async () => {
      const wrapper = await mountInEditMode()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointermove', 100, 100)

      expect(wrapper.find('.zone-rect').exists()).toBe(false)
      expect(wrapper.exists()).toBe(true)
    })
  })

  describe('camera switch', () => {
    it('adopts an externally-updated modelValue for the same camera without resetting the draft', async () => {
      const wrapper = mountPicker(null)
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()
      const overlay = wrapper.find('.picker-overlay')
      await firePointer(overlay.element, 'pointerdown', 40, 30)
      await firePointer(overlay.element, 'pointermove', 200, 150)
      await firePointer(overlay.element, 'pointerup', 200, 150)
      expect(wrapper.find('.zone-rect').exists()).toBe(true)

      // Same camera, but the parent re-synced a zone from elsewhere (e.g. a
      // page-level reload) — the in-progress draft must survive untouched.
      await wrapper.setProps({ camera: 'Driveway', modelValue: RECT_ZONE })
      await flushPromises()

      expect(wrapper.find('.zone-rect').exists()).toBe(true)
      expect(wrapper.text()).not.toContain('Loading recent frames')
    })

    it('resets the draft and re-evaluates mode/clips for the new camera', async () => {
      const wrapper = mountPicker(RECT_ZONE)
      await flushPromises()
      await wrapper.find('img.picker-image').trigger('load')
      await flushPromises()
      expect(wrapper.find('.zone-rect').exists()).toBe(true)

      await wrapper.setProps({ camera: 'Backyard', modelValue: null })
      await flushPromises()

      expect(wrapper.text()).toContain('No vehicle selected')
      expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('camera=Backyard'))).toBe(true)
    })

    it('ignores a slower, stale recent-clips response after a newer camera switch already resolved', async () => {
      let resolveFirst!: (r: Response) => void
      vi.stubGlobal(
        'fetch',
        vi.fn((url: string) => {
          if (url.includes('camera=Driveway')) {
            return new Promise<Response>((resolve) => {
              resolveFirst = resolve
            })
          }
          return Promise.resolve(jsonResponse([makeClip('newer')]))
        }),
      )
      const wrapper = mountPicker(null)
      await flushPromises()
      // Switch cameras before the first (slower) request resolves.
      await wrapper.setProps({ camera: 'Backyard' })
      await flushPromises()
      expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(1)

      // The stale Driveway response arrives late — must not clobber the
      // already-loaded Backyard clip list.
      resolveFirst(jsonResponse([makeClip('c1'), makeClip('c2')]))
      await flushPromises()

      expect(wrapper.findAll('.thumb-strip-item')).toHaveLength(1)
    })
  })
})
