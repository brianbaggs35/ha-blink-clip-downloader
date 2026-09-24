import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, type VueWrapper } from '@vue/test-utils'
import AssetZoneCanvas from './AssetZoneCanvas.vue'
import type { AssetZone } from '../../api/types'

const SIZE = { width: 400, height: 200 }

function mountCanvas(props: Partial<InstanceType<typeof AssetZoneCanvas>['$props']> = {}) {
  return mount(AssetZoneCanvas, {
    props: {
      src: '/frame.jpg',
      alt: 'Frame',
      tool: 'rect',
      modelValue: null,
      color: '#22d3ee',
      others: [],
      ...props,
    },
    attachTo: document.body,
  })
}

// A real PointerEvent carries clientX/clientY, which VTU's trigger() can't set
// on it (see VehicleZonePicker.spec.ts).
function pointer(wrapper: VueWrapper, type: string, x: number, y: number, button = 0) {
  const el = wrapper.find('[data-testid="zone-canvas-surface"]').element
  el.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: y, button, bubbles: true }))
}

async function drag(wrapper: VueWrapper, points: [number, number][]) {
  pointer(wrapper, 'pointerdown', ...points[0])
  for (const p of points.slice(1)) pointer(wrapper, 'pointermove', ...p)
  await wrapper.vm.$nextTick()
  pointer(wrapper, 'pointerup', ...points[points.length - 1])
  await wrapper.vm.$nextTick()
}

const emittedZones = (wrapper: VueWrapper) => (wrapper.emitted('update:modelValue') ?? []).map((e) => e[0] as AssetZone)

describe('AssetZoneCanvas', () => {
  beforeEach(() => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      right: SIZE.width,
      bottom: SIZE.height,
      x: 0,
      y: 0,
      toJSON: () => ({}),
      ...SIZE,
    } as DOMRect)
  })
  afterEach(() => {
    vi.restoreAllMocks()
    document.body.innerHTML = ''
  })

  it('draws a rectangle, normalized to the frame', async () => {
    const wrapper = mountCanvas()
    pointer(wrapper, 'pointerdown', 40, 20)
    pointer(wrapper, 'pointermove', 200, 120)
    await wrapper.vm.$nextTick()
    // The rectangle is drawn live, dashed, while the drag is in progress.
    expect(wrapper.find('.zone-canvas-surface').classes()).toContain('drawing')
    expect(wrapper.find('polygon').attributes('points')).toBe('10,10 50,10 50,60 10,60')
    pointer(wrapper, 'pointerup', 200, 120)
    await wrapper.vm.$nextTick()
    expect(emittedZones(wrapper)).toEqual([{ shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.5, y_max: 0.6 }])
  })

  it('keeps a drag that leaves the frame pinned to its edge', async () => {
    const wrapper = mountCanvas()
    await drag(wrapper, [
      [300, 100],
      [900, -50],
    ])
    expect(emittedZones(wrapper)).toEqual([{ shape: 'rect', x_min: 0.75, y_min: 0, x_max: 1, y_max: 0.5 }])
  })

  it('treats a click, or a right-click, as nothing', async () => {
    const wrapper = mountCanvas({ modelValue: { shape: 'rect', x_min: 0.1, y_min: 0.1, x_max: 0.2, y_max: 0.2 } })
    await drag(wrapper, [
      [300, 150],
      [301, 151],
    ])
    await drag(wrapper, [[300, 150]])
    pointer(wrapper, 'pointerdown', 300, 150, 2)
    pointer(wrapper, 'pointermove', 380, 190)
    pointer(wrapper, 'pointerup', 380, 190)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('moves and resizes the rectangle it already has', async () => {
    const zone: AssetZone = { shape: 'rect', x_min: 0.25, y_min: 0.25, x_max: 0.5, y_max: 0.75 }
    const wrapper = mountCanvas({ modelValue: zone })
    expect(wrapper.findAll('.zone-canvas-handle')).toHaveLength(4)
    // Grab the body (moves it)...
    await drag(wrapper, [
      [150, 100],
      [190, 110],
    ])
    // ...then the bottom-right corner (resizes it).
    await drag(wrapper, [
      [200, 150],
      [240, 180],
    ])
    const [moved, resized] = emittedZones(wrapper)
    expect(moved).toEqual({ shape: 'rect', x_min: 0.35, y_min: 0.3, x_max: 0.6, y_max: 0.8 })
    expect(resized).toEqual({ shape: 'rect', x_min: 0.25, y_min: 0.25, x_max: 0.6, y_max: 0.9 })
  })

  it('traces a freeform outline', async () => {
    const wrapper = mountCanvas({ tool: 'polygon' })
    expect(wrapper.find('.zone-canvas-surface').classes()).toContain('lasso')
    pointer(wrapper, 'pointerdown', 40, 40)
    await wrapper.vm.$nextTick()
    // One point is not an outline yet.
    expect(wrapper.find('polygon').exists()).toBe(false)
    pointer(wrapper, 'pointermove', 200, 40)
    pointer(wrapper, 'pointermove', 200, 160)
    await wrapper.vm.$nextTick()
    expect(wrapper.find('polygon').attributes('points')).toBe('10,20 50,20 50,80')
    pointer(wrapper, 'pointerup', 200, 160)
    await wrapper.vm.$nextTick()
    expect(emittedZones(wrapper)).toEqual([
      {
        shape: 'polygon',
        points: [
          [0.1, 0.2],
          [0.5, 0.2],
          [0.5, 0.8],
        ],
      },
    ])
    // No corner handles on a freeform zone.
    await wrapper.setProps({ modelValue: emittedZones(wrapper)[0] })
    expect(wrapper.findAll('.zone-canvas-handle')).toHaveLength(0)
  })

  it('ignores a scribble too small to mean anything', async () => {
    const wrapper = mountCanvas({ tool: 'polygon' })
    await drag(wrapper, [
      [40, 40],
      [45, 42],
      [42, 46],
    ])
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('abandons the gesture on Escape, or when the browser cancels it', async () => {
    const wrapper = mountCanvas()
    pointer(wrapper, 'pointerdown', 40, 20)
    pointer(wrapper, 'pointermove', 200, 120)
    const escape = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })
    const stop = vi.spyOn(escape, 'stopPropagation')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    window.dispatchEvent(escape)
    expect(stop).toHaveBeenCalled()
    pointer(wrapper, 'pointermove', 250, 150)
    pointer(wrapper, 'pointerup', 250, 150)

    pointer(wrapper, 'pointerdown', 40, 20)
    pointer(wrapper, 'pointermove', 200, 120)
    pointer(wrapper, 'pointercancel', 200, 120)
    pointer(wrapper, 'pointerup', 200, 120)
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('keeps Escape from the dialog around it while drawing, and only then', async () => {
    const wrapper = mountCanvas()
    const dialogHeard = vi.fn()
    document.addEventListener('keydown', dialogHeard)
    pointer(wrapper, 'pointerdown', 40, 20)
    pointer(wrapper, 'pointermove', 200, 120)
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(dialogHeard).not.toHaveBeenCalled()
    // The gesture is over, so the next Escape is the dialog's again.
    document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    expect(dialogHeard).toHaveBeenCalledTimes(1)
    document.removeEventListener('keydown', dialogHeard)
  })

  it('does nothing on a surface with no size yet', () => {
    vi.mocked(HTMLElement.prototype.getBoundingClientRect).mockReturnValue({ width: 0, height: 0 } as DOMRect)
    const wrapper = mountCanvas()
    pointer(wrapper, 'pointerdown', 10, 10)
    pointer(wrapper, 'pointermove', 50, 50)
    pointer(wrapper, 'pointerup', 50, 50)
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('passes the frame loading, or failing, up', async () => {
    const wrapper = mountCanvas()
    await wrapper.find('img').trigger('load')
    await wrapper.find('img').trigger('error')
    expect(wrapper.emitted('load')).toHaveLength(1)
    expect(wrapper.emitted('error')).toHaveLength(1)
  })

  it('stops listening for Escape once it is gone', () => {
    const removed = vi.spyOn(window, 'removeEventListener')
    const wrapper = mountCanvas()
    pointer(wrapper, 'pointerdown', 40, 20)
    wrapper.unmount()
    expect(removed).toHaveBeenCalledWith('keydown', expect.any(Function), { capture: true })
  })

  it('describes the drawing area for each tool', async () => {
    const wrapper = mountCanvas()
    const surface = () => wrapper.find('[data-testid="zone-canvas-surface"]')
    expect(surface().attributes('aria-label')).toContain('rectangle')
    await wrapper.setProps({ tool: 'polygon' })
    expect(surface().attributes('aria-label')).toContain('trace')
  })
})
