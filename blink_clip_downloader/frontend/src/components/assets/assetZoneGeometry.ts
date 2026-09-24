// Rendering helpers for marked-asset zones. Zones are stored normalized
// (0-1, see protected_assets.normalize_zone) and drawn into an SVG whose
// viewBox is 0-100 on both axes with preserveAspectRatio="none" — so they
// scale with the picture on their own, with no pixel measuring, and stay
// right when a dialog is resized or a phone is rotated.

import type { AssetZone } from '../../api/types'

/** One zone as the overlay draws it. */
export interface OverlayZone {
  id: string
  name: string
  zone: AssetZone
  color: string
  /** Switched off: shown, but faded, so it's clear it isn't being watched. */
  muted?: boolean
}

export interface ZoneBounds {
  x_min: number
  y_min: number
  x_max: number
  y_max: number
}

/** The axis-aligned bounds of *zone*. */
export function zoneBounds(zone: AssetZone): ZoneBounds {
  if (zone.shape === 'rect') {
    return { x_min: zone.x_min, y_min: zone.y_min, x_max: zone.x_max, y_max: zone.y_max }
  }
  const xs = zone.points.map((p) => p[0])
  const ys = zone.points.map((p) => p[1])
  return { x_min: Math.min(...xs), y_min: Math.min(...ys), x_max: Math.max(...xs), y_max: Math.max(...ys) }
}

/** *zone*'s outline as an SVG `points` value in the 0-100 viewBox. */
export function zoneSvgPoints(zone: AssetZone): string {
  const ring: [number, number][] =
    zone.shape === 'rect'
      ? [
          [zone.x_min, zone.y_min],
          [zone.x_max, zone.y_min],
          [zone.x_max, zone.y_max],
          [zone.x_min, zone.y_max],
        ]
      : zone.points
  return ring.map(([x, y]) => `${round(x * 100)},${round(y * 100)}`).join(' ')
}

/** Share of the frame's height a label needs above a zone; a zone nearer
 * the top than this gets its label inside its own top edge instead. */
const LABEL_HEADROOM = 0.09

/** Share of the frame's width past which a label is anchored to the zone's
 * right edge rather than its left, so a zone at the right of the frame
 * doesn't push its name off the picture. */
const LABEL_RIGHT_ANCHOR = 0.65

/** Where a zone's name label sits: just above the zone's top-left corner,
 * moved inside the zone when it touches the top of the frame and anchored
 * to its right edge near the right of the frame, so a label never hangs off
 * the picture. */
export function zoneLabelStyle(zone: AssetZone): Record<string, string> {
  const b = zoneBounds(zone)
  const style: Record<string, string> = {
    top: `${round(b.y_min * 100)}%`,
    transform: b.y_min < LABEL_HEADROOM ? 'none' : 'translateY(-100%)',
  }
  if (b.x_min > LABEL_RIGHT_ANCHOR) style.right = `${round((1 - b.x_max) * 100)}%`
  else style.left = `${round(b.x_min * 100)}%`
  return style
}

/** In which part of the frame a zone sits, in the words the analysis prompt
 * uses for it (security/vehicles.py's describe_region). */
export function zoneRegion(zone: AssetZone): string {
  const b = zoneBounds(zone)
  const band = (v: number, low: string, mid: string, high: string) => {
    if (v < 1 / 3) return low
    return v < 2 / 3 ? mid : high
  }
  const vertical = band((b.y_min + b.y_max) / 2, 'upper', 'middle', 'lower')
  const horizontal = band((b.x_min + b.x_max) / 2, 'left', 'centre', 'right')
  if (vertical === 'middle' && horizontal === 'centre') return 'centre of the frame'
  if (vertical === 'middle') return `${horizontal} of the frame`
  if (horizontal === 'centre') return `${vertical} centre of the frame`
  return `${vertical} ${horizontal} of the frame`
}

function round(value: number): number {
  return Math.round(value * 100) / 100
}
