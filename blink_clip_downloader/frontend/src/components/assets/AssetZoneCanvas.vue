<script setup lang="ts">
// Drawing one asset's zone on a camera frame: a rectangle (drag to draw,
// then drag its body or corners to adjust) or a freeform lasso (press, trace,
// release). The other assets on the same camera are shown faintly beneath,
// so a new one can be placed beside them rather than guessed at.
//
// The zone itself is always normalized (0-1). Pixel arithmetic happens only
// for the length of one gesture, against the surface's size at that moment,
// and reuses the Vehicles tab's tested pixel helpers — so a dialog resized
// or a phone rotated between gestures can never leave a zone drawn at the
// old size.
import { computed, onBeforeUnmount, ref } from 'vue'
import type { AssetZone } from '../../api/types'
import AssetZoneOverlay from './AssetZoneOverlay.vue'
import { type OverlayZone, zoneSvgPoints } from './assetZoneGeometry'
import {
  type CornerHandle,
  type Point,
  type Rect,
  addFreeformPoint,
  clampRect,
  fractionToRect,
  hitTest,
  moveRect,
  polygonToFraction,
  rectFromPoints,
  rectToFraction,
  resizeRect,
} from '../vehicles/vehicleZoneGeometry'

const props = defineProps<{
  src: string
  alt: string
  tool: 'rect' | 'polygon'
  modelValue: AssetZone | null
  color: string
  others: OverlayZone[]
}>()
const emit = defineEmits<{ 'update:modelValue': [AssetZone]; load: []; error: [] }>()

const surface = ref<HTMLDivElement | null>(null)

interface GestureStart {
  width: number
  height: number
  startX: number
  startY: number
}
type Gesture = GestureStart &
  ({ kind: 'draw' } | { kind: 'move'; origRect: Rect } | { kind: 'resize'; handle: CornerHandle })
const gesture = ref<Gesture | null>(null)
const draftRect = ref<Rect | null>(null)
const draftPath = ref<Point[]>([])

/** The pointer's position on the surface in pixels, kept inside it — a drag
 * that leaves the frame pins the zone to its edge rather than past it. */
function localPoint(e: PointerEvent, box: DOMRect): Point {
  const x = Math.min(Math.max(e.clientX - box.left, 0), box.width)
  const y = Math.min(Math.max(e.clientY - box.top, 0), box.height)
  return [x, y]
}

function currentRect(width: number, height: number): Rect | null {
  const zone = props.modelValue
  if (!zone || zone.shape !== 'rect') return null
  return fractionToRect(zone, width, height)
}

function onPointerDown(e: PointerEvent) {
  // Only the primary button — a right-click must not start a zone.
  if (e.button > 0 || !surface.value) return
  const box = surface.value.getBoundingClientRect()
  if (box.width <= 0 || box.height <= 0) return
  const [x, y] = localPoint(e, box)
  const base = { width: box.width, height: box.height, startX: x, startY: y }
  if (props.tool === 'polygon') {
    gesture.value = { kind: 'draw', ...base }
    draftPath.value = [[x, y]]
  } else {
    const rect = currentRect(box.width, box.height)
    const handle = rect ? hitTest(x, y, rect) : null
    if (rect && handle) {
      gesture.value =
        handle === 'move' ? { kind: 'move', ...base, origRect: rect } : { kind: 'resize', ...base, handle }
      draftRect.value = rect
    } else {
      gesture.value = { kind: 'draw', ...base }
      draftRect.value = { x, y, width: 0, height: 0 }
    }
  }
  ;(e.target as Element).setPointerCapture?.(e.pointerId)
  window.addEventListener('keydown', onKeydown)
}

function onPointerMove(e: PointerEvent) {
  const g = gesture.value
  if (!g || !surface.value) return
  const [x, y] = localPoint(e, surface.value.getBoundingClientRect())
  if (props.tool === 'polygon') {
    draftPath.value = addFreeformPoint(draftPath.value, [x, y])
  } else if (g.kind === 'draw') {
    draftRect.value = clampRect(rectFromPoints(g.startX, g.startY, x, y), g.width, g.height)
  } else if (g.kind === 'resize') {
    // A resize starts from the rectangle it grabbed (see onPointerDown).
    draftRect.value = clampRect(resizeRect(draftRect.value as Rect, g.handle, x, y), g.width, g.height)
  } else {
    draftRect.value = moveRect(g.origRect, x - g.startX, y - g.startY, g.width, g.height)
  }
}

function onPointerUp() {
  const g = gesture.value
  if (!g) return
  // A click, or a scribble too small to be meant, keeps whatever zone was
  // there: it is not a request to delete it. Clear exists for that.
  const zone =
    props.tool === 'polygon'
      ? polygonToFraction(draftPath.value, g.width, g.height)
      : draftRect.value && rectToFraction(draftRect.value, g.width, g.height)
  endGesture()
  if (zone) emit('update:modelValue', zone)
}

function endGesture() {
  gesture.value = null
  draftRect.value = null
  draftPath.value = []
  window.removeEventListener('keydown', onKeydown)
}

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  // Abandon the gesture in progress, not the dialog around it.
  e.stopPropagation()
  endGesture()
}

onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

/** What to draw: the gesture in progress, else the zone as it stands. */
const shown = computed<AssetZone | null>(() => {
  const g = gesture.value
  if (g && props.tool === 'polygon') {
    if (draftPath.value.length < 2) return null
    return { shape: 'polygon', points: draftPath.value.map(([x, y]) => [x / g.width, y / g.height]) }
  }
  if (g && draftRect.value) {
    const r = draftRect.value
    return {
      shape: 'rect',
      x_min: r.x / g.width,
      y_min: r.y / g.height,
      x_max: (r.x + r.width) / g.width,
      y_max: (r.y + r.height) / g.height,
    }
  }
  return props.modelValue
})

const handles = computed(() => {
  const zone = shown.value
  if (props.tool !== 'rect' || !zone || zone.shape !== 'rect') return []
  return [
    { id: 'nw', left: zone.x_min, top: zone.y_min },
    { id: 'ne', left: zone.x_max, top: zone.y_min },
    { id: 'sw', left: zone.x_min, top: zone.y_max },
    { id: 'se', left: zone.x_max, top: zone.y_max },
  ].map((h) => ({ id: h.id, style: { left: `${h.left * 100}%`, top: `${h.top * 100}%` } }))
})
</script>

<template>
  <div class="zone-canvas" :style="{ '--zone-color': color }">
    <img
      :src="src"
      :alt="alt"
      class="zone-canvas-image"
      draggable="false"
      @load="emit('load')"
      @error="emit('error')"
    />
    <AssetZoneOverlay :zones="others" ghost />
    <div
      ref="surface"
      class="zone-canvas-surface"
      :class="{ lasso: tool === 'polygon', drawing: gesture !== null }"
      data-testid="zone-canvas-surface"
      role="application"
      :aria-label="
        tool === 'rect'
          ? 'Drawing area: drag to draw a rectangle around the asset'
          : 'Drawing area: press and trace around the asset, then release'
      "
      @pointerdown="onPointerDown"
      @pointermove="onPointerMove"
      @pointerup="onPointerUp"
      @pointercancel="endGesture"
    >
      <svg class="zone-canvas-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
        <polygon
          v-if="shown"
          :points="zoneSvgPoints(shown)"
          class="zone-canvas-shape"
          vector-effect="non-scaling-stroke"
        />
      </svg>
      <span v-for="handle in handles" :key="handle.id" class="zone-canvas-handle" :style="handle.style" />
    </div>
  </div>
</template>

<style scoped>
.zone-canvas {
  position: relative;
  width: 100%;
  user-select: none;
  -webkit-user-select: none;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--card2);
  line-height: 0;
}

.zone-canvas-image {
  display: block;
  width: 100%;
  height: auto;
  pointer-events: none;
}

.zone-canvas-surface {
  position: absolute;
  inset: 0;
  cursor: crosshair;
  /* A finger drawing on a phone must draw, not scroll the dialog. */
  touch-action: none;
}

.zone-canvas-surface.lasso {
  cursor: cell;
}

.zone-canvas-svg {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
  pointer-events: none;
}

.zone-canvas-shape {
  fill: color-mix(in srgb, var(--zone-color) 26%, transparent);
  stroke: var(--zone-color);
  stroke-width: 2.5;
  stroke-linejoin: round;
}

.drawing .zone-canvas-shape {
  stroke-dasharray: 6 4;
}

.zone-canvas-handle {
  position: absolute;
  width: 14px;
  height: 14px;
  margin: -7px 0 0 -7px;
  border-radius: 50%;
  background: var(--zone-color);
  border: 2px solid #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
  pointer-events: none;
}

@media (pointer: coarse) {
  /* Bigger targets for a fingertip; the grab radius itself is generous. */
  .zone-canvas-handle {
    width: 20px;
    height: 20px;
    margin: -10px 0 0 -10px;
  }
}
</style>
