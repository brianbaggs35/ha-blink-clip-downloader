import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AssetZoneOverlay from './AssetZoneOverlay.vue'
import type { OverlayZone } from './assetZoneGeometry'

const ZONES: OverlayZone[] = [
  {
    id: 'a',
    name: 'Front door',
    color: '#22d3ee',
    zone: { shape: 'rect', x_min: 0.1, y_min: 0.2, x_max: 0.3, y_max: 0.8 },
  },
  {
    id: 'b',
    name: 'Bike',
    color: '#f59e0b',
    muted: true,
    zone: {
      shape: 'polygon',
      points: [
        [0.6, 0.5],
        [0.9, 0.5],
        [0.75, 0.9],
      ],
    },
  },
]

describe('AssetZoneOverlay', () => {
  it('draws an outline and a name for every zone', () => {
    const wrapper = mount(AssetZoneOverlay, { props: { zones: ZONES } })
    const shapes = wrapper.findAll('polygon')
    expect(shapes.map((s) => s.attributes('points'))).toEqual(['10,20 30,20 30,80 10,80', '60,50 90,50 75,90'])
    expect(shapes[1].classes()).toContain('muted')
    // Static labels: nothing under a drawing tool may catch the pointer.
    expect(wrapper.findAll('button')).toHaveLength(0)
    expect(wrapper.findAll('.zone-label').map((l) => l.text())).toEqual(['Front door', 'Bike'])
  })

  it('lights one zone and fades the rest', () => {
    const wrapper = mount(AssetZoneOverlay, { props: { zones: ZONES, highlighted: 'a', interactive: true } })
    const shapes = wrapper.findAll('polygon')
    expect(shapes[0].classes()).toContain('highlighted')
    expect(shapes[1].classes()).toContain('dim')
    expect(wrapper.findAll('button')[1].classes()).toContain('dim')
  })

  it('reports hovering, focusing and choosing a label', async () => {
    const wrapper = mount(AssetZoneOverlay, { props: { zones: ZONES, interactive: true } })
    const label = wrapper.findAll('button')[0]
    expect(label.attributes('aria-label')).toBe('Select Front door')
    await label.trigger('mouseenter')
    await label.trigger('mouseleave')
    await label.trigger('focus')
    await label.trigger('blur')
    await label.trigger('click')
    expect(wrapper.emitted('hover')).toEqual([['a'], [null], ['a'], [null]])
    expect(wrapper.emitted('select')).toEqual([['a']])
  })

  it('draws the other assets faintly while one is being drawn', () => {
    const wrapper = mount(AssetZoneOverlay, { props: { zones: ZONES, ghost: true } })
    expect(wrapper.find('.asset-zone-overlay').classes()).toContain('ghost')
  })
})
