import { describe, expect, it } from 'vitest'
import {
  clampRect,
  fractionToRect,
  hitTest,
  moveRect,
  rectFromPoints,
  rectToFraction,
  resizeRect,
} from './vehicleZoneGeometry'

describe('rectFromPoints', () => {
  it('normalizes a forward drag (top-left to bottom-right)', () => {
    expect(rectFromPoints(10, 20, 50, 80)).toEqual({ x: 10, y: 20, width: 40, height: 60 })
  })

  it('normalizes a reverse drag (bottom-right to top-left)', () => {
    expect(rectFromPoints(50, 80, 10, 20)).toEqual({ x: 10, y: 20, width: 40, height: 60 })
  })

  it('normalizes a drag that only reverses one axis', () => {
    expect(rectFromPoints(50, 20, 10, 80)).toEqual({ x: 10, y: 20, width: 40, height: 60 })
  })
})

describe('clampRect', () => {
  it('leaves a rectangle already inside the container unchanged', () => {
    const rect = { x: 10, y: 10, width: 20, height: 20 }
    expect(clampRect(rect, 100, 100)).toEqual(rect)
  })

  it('pulls a rectangle back inside when it overflows the right/bottom edge', () => {
    expect(clampRect({ x: 90, y: 90, width: 20, height: 20 }, 100, 100)).toEqual({
      x: 80,
      y: 80,
      width: 20,
      height: 20,
    })
  })

  it('pulls a rectangle back inside when it overflows the left/top edge', () => {
    expect(clampRect({ x: -10, y: -10, width: 20, height: 20 }, 100, 100)).toEqual({
      x: 0,
      y: 0,
      width: 20,
      height: 20,
    })
  })

  it('shrinks a rectangle larger than the container itself', () => {
    expect(clampRect({ x: 0, y: 0, width: 150, height: 150 }, 100, 100)).toEqual({
      x: 0,
      y: 0,
      width: 100,
      height: 100,
    })
  })
})

describe('rectToFraction / fractionToRect round trip', () => {
  it('converts a pixel rect to 0-1 fractions', () => {
    expect(rectToFraction({ x: 25, y: 50, width: 50, height: 100 }, 100, 200)).toEqual({
      x_min: 0.25,
      y_min: 0.25,
      x_max: 0.75,
      y_max: 0.75,
    })
  })

  it('returns null for a rectangle smaller than MIN_RECT_SIZE (accidental click, not a drag)', () => {
    expect(rectToFraction({ x: 10, y: 10, width: 2, height: 2 }, 100, 100)).toBeNull()
  })

  it('returns null when the container has no measured size yet', () => {
    expect(rectToFraction({ x: 10, y: 10, width: 20, height: 20 }, 0, 0)).toBeNull()
  })

  it('round-trips back to the original pixel rect via fractionToRect', () => {
    const original = { x: 25, y: 50, width: 50, height: 100 }
    const frac = rectToFraction(original, 100, 200)!
    expect(fractionToRect(frac, 100, 200)).toEqual(original)
  })
})

describe('hitTest', () => {
  const rect = { x: 100, y: 100, width: 80, height: 60 }

  it('detects each corner handle within grab radius', () => {
    expect(hitTest(100, 100, rect)).toBe('nw')
    expect(hitTest(180, 100, rect)).toBe('ne')
    expect(hitTest(100, 160, rect)).toBe('sw')
    expect(hitTest(180, 160, rect)).toBe('se')
  })

  it('prefers a corner handle over "move" when both would technically match', () => {
    // A point exactly on the nw corner is also technically inside the body.
    expect(hitTest(101, 101, rect)).toBe('nw')
  })

  it('detects the body as "move" when not near any corner', () => {
    expect(hitTest(140, 130, rect)).toBe('move')
  })

  it('returns null when the point is outside the rectangle entirely', () => {
    expect(hitTest(5, 5, rect)).toBeNull()
  })
})

describe('resizeRect', () => {
  const rect = { x: 100, y: 100, width: 80, height: 60 }

  it('dragging the se handle keeps the nw corner anchored', () => {
    expect(resizeRect(rect, 'se', 220, 200)).toEqual({ x: 100, y: 100, width: 120, height: 100 })
  })

  it('dragging the nw handle keeps the se corner anchored', () => {
    expect(resizeRect(rect, 'nw', 50, 50)).toEqual({ x: 50, y: 50, width: 130, height: 110 })
  })

  it('dragging a handle past the opposite corner flips the rectangle correctly', () => {
    // Dragging se up-and-left past its anchored nw corner (100, 100) must
    // still produce a positive-size rectangle, not a negative width/height.
    expect(resizeRect(rect, 'se', 50, 50)).toEqual({ x: 50, y: 50, width: 50, height: 50 })
  })
})

describe('moveRect', () => {
  it('translates by (dx, dy) when it stays in bounds', () => {
    expect(moveRect({ x: 10, y: 10, width: 20, height: 20 }, 5, -5, 100, 100)).toEqual({
      x: 15,
      y: 5,
      width: 20,
      height: 20,
    })
  })

  it('clamps the move so the rectangle cannot leave the container', () => {
    expect(moveRect({ x: 90, y: 10, width: 20, height: 20 }, 50, 0, 100, 100)).toEqual({
      x: 80,
      y: 10,
      width: 20,
      height: 20,
    })
  })
})
