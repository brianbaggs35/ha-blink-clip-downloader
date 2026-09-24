import { describe, expect, it } from 'vitest'
import type { AssetZone } from '../../api/types'
import { zoneBounds, zoneLabelStyle, zoneRegion, zoneSvgPoints } from './assetZoneGeometry'

const rect = (x_min: number, y_min: number, x_max: number, y_max: number): AssetZone => ({
  shape: 'rect',
  x_min,
  y_min,
  x_max,
  y_max,
})

const TRIANGLE: AssetZone = {
  shape: 'polygon',
  points: [
    [0.2, 0.3],
    [0.6, 0.25],
    [0.4, 0.9],
  ],
}

describe('assetZoneGeometry', () => {
  it('bounds a rectangle and a freeform outline', () => {
    expect(zoneBounds(rect(0.1, 0.2, 0.3, 0.4))).toEqual({ x_min: 0.1, y_min: 0.2, x_max: 0.3, y_max: 0.4 })
    expect(zoneBounds(TRIANGLE)).toEqual({ x_min: 0.2, y_min: 0.25, x_max: 0.6, y_max: 0.9 })
  })

  it('outlines a zone in the 0-100 viewBox', () => {
    expect(zoneSvgPoints(rect(0.1, 0.2, 0.3, 0.4))).toBe('10,20 30,20 30,40 10,40')
    expect(zoneSvgPoints(TRIANGLE)).toBe('20,30 60,25 40,90')
  })

  it('puts a label above the zone, inside it at the top edge, and right-anchored near the right', () => {
    expect(zoneLabelStyle(rect(0.1, 0.5, 0.3, 0.8))).toEqual({
      top: '50%',
      transform: 'translateY(-100%)',
      left: '10%',
    })
    expect(zoneLabelStyle(rect(0.1, 0.02, 0.3, 0.8))).toMatchObject({ transform: 'none' })
    expect(zoneLabelStyle(rect(0.7, 0.5, 0.95, 0.8))).toEqual({
      top: '50%',
      transform: 'translateY(-100%)',
      right: '5%',
    })
  })

  it('names the part of the frame a zone is in, the way the prompt does', () => {
    expect(zoneRegion(rect(0.4, 0.4, 0.6, 0.6))).toBe('centre of the frame')
    expect(zoneRegion(rect(0.0, 0.4, 0.2, 0.6))).toBe('left of the frame')
    expect(zoneRegion(rect(0.4, 0.8, 0.6, 1.0))).toBe('lower centre of the frame')
    expect(zoneRegion(rect(0.7, 0.0, 0.9, 0.2))).toBe('upper right of the frame')
  })
})
