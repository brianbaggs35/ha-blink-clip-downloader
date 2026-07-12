// Pure geometry helpers for VehicleZonePicker's click-drag rectangle
// selector — deliberately DOM-free so the actual math (drag-to-rectangle,
// pixel<->fraction conversion, corner-handle hit-testing, resize/move) can be
// unit tested directly without a real <canvas> 2D context, which jsdom does
// not implement.

import type { CarZone } from '../../api/types'

export interface Rect {
  x: number
  y: number
  width: number
  height: number
}

export type CornerHandle = 'nw' | 'ne' | 'sw' | 'se'
export type Handle = CornerHandle | 'move'

/** Smallest rectangle (in pixels) worth keeping — guards against an
 * accidental click-without-drag producing a zero/near-zero-size zone. */
export const MIN_RECT_SIZE = 6

/** Distance (in pixels) from a corner that still counts as grabbing its
 * resize handle, rather than the rectangle's body (move) or empty space
 * (draw a new rectangle). */
export const HANDLE_GRAB_RADIUS = 12

/** Normalizes two drag endpoints into a rectangle with a positive width and
 * height, regardless of which direction the drag went. */
export function rectFromPoints(x1: number, y1: number, x2: number, y2: number): Rect {
  return {
    x: Math.min(x1, x2),
    y: Math.min(y1, y2),
    width: Math.abs(x2 - x1),
    height: Math.abs(y2 - y1),
  }
}

/** Clamps a rectangle so it stays fully within a `containerWidth` x
 * `containerHeight` box, preserving its size where possible. */
export function clampRect(rect: Rect, containerWidth: number, containerHeight: number): Rect {
  const width = Math.min(rect.width, containerWidth)
  const height = Math.min(rect.height, containerHeight)
  const x = Math.min(Math.max(rect.x, 0), containerWidth - width)
  const y = Math.min(Math.max(rect.y, 0), containerHeight - height)
  return { x, y, width, height }
}

/** Converts a pixel-space rectangle to the 0-1 fraction shape the backend's
 * car_zone already expects (`_normalize_car_zone` in media_server.py) — or
 * `null` if it's too small to be an intentional selection. */
export function rectToFraction(rect: Rect, containerWidth: number, containerHeight: number): CarZone | null {
  if (rect.width < MIN_RECT_SIZE || rect.height < MIN_RECT_SIZE) return null
  if (containerWidth <= 0 || containerHeight <= 0) return null
  return {
    x_min: rect.x / containerWidth,
    y_min: rect.y / containerHeight,
    x_max: (rect.x + rect.width) / containerWidth,
    y_max: (rect.y + rect.height) / containerHeight,
  }
}

/** Inverse of {@link rectToFraction} — used to render an already-saved zone
 * back onto the picker as pixels for the currently-displayed frame's size. */
export function fractionToRect(frac: CarZone, containerWidth: number, containerHeight: number): Rect {
  return {
    x: frac.x_min * containerWidth,
    y: frac.y_min * containerHeight,
    width: (frac.x_max - frac.x_min) * containerWidth,
    height: (frac.y_max - frac.y_min) * containerHeight,
  }
}

/** Determines what a pointer at (px, py) would grab on the current
 * rectangle: a corner resize handle, the body (move), or nothing (draw a
 * new rectangle from scratch). */
export function hitTest(px: number, py: number, rect: Rect): Handle | null {
  const corners: { handle: Handle; x: number; y: number }[] = [
    { handle: 'nw', x: rect.x, y: rect.y },
    { handle: 'ne', x: rect.x + rect.width, y: rect.y },
    { handle: 'sw', x: rect.x, y: rect.y + rect.height },
    { handle: 'se', x: rect.x + rect.width, y: rect.y + rect.height },
  ]
  for (const c of corners) {
    if (Math.hypot(px - c.x, py - c.y) <= HANDLE_GRAB_RADIUS) return c.handle
  }
  if (px >= rect.x && px <= rect.x + rect.width && py >= rect.y && py <= rect.y + rect.height) {
    return 'move'
  }
  return null
}

/** Recomputes a rectangle while dragging corner `handle` to a new pixel
 * position — the opposite corner stays anchored in place. */
export function resizeRect(rect: Rect, handle: CornerHandle, px: number, py: number): Rect {
  const opposite: Record<CornerHandle, { x: number; y: number }> = {
    nw: { x: rect.x + rect.width, y: rect.y + rect.height },
    ne: { x: rect.x, y: rect.y + rect.height },
    sw: { x: rect.x + rect.width, y: rect.y },
    se: { x: rect.x, y: rect.y },
  }
  const anchor = opposite[handle]
  return rectFromPoints(anchor.x, anchor.y, px, py)
}

/** Translates a rectangle by (dx, dy), clamped to stay inside the container. */
export function moveRect(rect: Rect, dx: number, dy: number, containerWidth: number, containerHeight: number): Rect {
  return clampRect({ ...rect, x: rect.x + dx, y: rect.y + dy }, containerWidth, containerHeight)
}
