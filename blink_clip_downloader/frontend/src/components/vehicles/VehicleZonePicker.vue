<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import { clipThumbUrl, listClips } from '../../api/clips'
import type { CarZone, ClipListItem } from '../../api/types'
import {
  type CornerHandle,
  type Rect,
  clampRect,
  fractionToRect,
  hitTest,
  moveRect,
  rectFromPoints,
  rectToFraction,
  resizeRect,
} from './vehicleZoneGeometry'

const props = defineProps<{ camera: string; modelValue: CarZone | null }>()
const emit = defineEmits<{ 'update:modelValue': [CarZone | null] }>()

const loading = ref(true)
const loadError = ref(false)
const recentClips = ref<ClipListItem[]>([])
const selectedClipId = ref('')

const containerEl = ref<HTMLDivElement | null>(null)
const containerSize = ref({ width: 0, height: 0 })

const rect = ref<Rect | null>(null)
type DragMode = {
  kind: 'draw' | 'move' | 'resize'
  handle?: CornerHandle
  startX: number
  startY: number
  origRect?: Rect
}
const drag = ref<DragMode | null>(null)

// Same request-sequencing guard as EnrollFromClipPicker.vue/LibraryPage.vue:
// only apply a response if it's still the most recently fired call by the
// time it resolves, so a stale, slower response can't overwrite a newer
// selection's data.
let clipsSeq = 0

async function loadRecentClips() {
  const seq = ++clipsSeq
  loading.value = true
  loadError.value = false
  try {
    const result = await listClips({ camera: props.camera, limit: 8, sort: 'newest' })
    if (seq !== clipsSeq) return
    recentClips.value = result
    if (recentClips.value.length) selectedClipId.value = recentClips.value[0].id
  } catch {
    if (seq === clipsSeq) loadError.value = true
  } finally {
    if (seq === clipsSeq) loading.value = false
  }
}
onMounted(loadRecentClips)
watch(() => props.camera, loadRecentClips)

function measureContainer() {
  if (!containerEl.value) return
  const box = containerEl.value.getBoundingClientRect()
  containerSize.value = { width: box.width, height: box.height }
}

function initRectFromModelValue() {
  if (props.modelValue && containerSize.value.width && containerSize.value.height) {
    rect.value = fractionToRect(props.modelValue, containerSize.value.width, containerSize.value.height)
  } else {
    rect.value = null
  }
}

async function onImageLoad() {
  await nextTick()
  measureContainer()
  initRectFromModelValue()
}

watch(
  () => props.modelValue,
  () => initRectFromModelValue(),
)

// Computed from clientX/clientY and the container's own bounding rect,
// rather than the pointer event's own offsetX/offsetY — offsetX/offsetY are
// relative to whichever element is the *current* event target, which can
// shift once setPointerCapture() retargets subsequent move/up events, and
// (unlike clientX/clientY) can't be set via a synthetic event's init dict
// for testing.
function pointerPos(e: PointerEvent): { x: number; y: number } {
  const box = containerEl.value?.getBoundingClientRect()
  return { x: e.clientX - (box?.left ?? 0), y: e.clientY - (box?.top ?? 0) }
}

function onPointerDown(e: PointerEvent) {
  const { x, y } = pointerPos(e)
  const handle = rect.value ? hitTest(x, y, rect.value) : null
  if (handle === 'move') {
    drag.value = { kind: 'move', startX: x, startY: y, origRect: rect.value! }
  } else if (handle) {
    drag.value = { kind: 'resize', handle, startX: x, startY: y }
  } else {
    drag.value = { kind: 'draw', startX: x, startY: y }
    rect.value = { x, y, width: 0, height: 0 }
  }
  ;(e.target as Element).setPointerCapture?.(e.pointerId)
}

function onPointerMove(e: PointerEvent) {
  if (!drag.value) return
  const { x, y } = pointerPos(e)
  const { width, height } = containerSize.value
  if (drag.value.kind === 'draw') {
    rect.value = clampRect(rectFromPoints(drag.value.startX, drag.value.startY, x, y), width, height)
  } else if (drag.value.kind === 'resize' && rect.value) {
    rect.value = clampRect(resizeRect(rect.value, drag.value.handle!, x, y), width, height)
  } else if (drag.value.kind === 'move' && drag.value.origRect) {
    rect.value = moveRect(drag.value.origRect, x - drag.value.startX, y - drag.value.startY, width, height)
  }
}

function commit() {
  const { width, height } = containerSize.value
  const frac = rect.value ? rectToFraction(rect.value, width, height) : null
  if (!frac) rect.value = null
  emit('update:modelValue', frac)
}

function onPointerUp() {
  if (!drag.value) return
  drag.value = null
  commit()
}

function clearZone() {
  rect.value = null
  emit('update:modelValue', null)
}

const rectStyle = computed(() => {
  if (!rect.value) return null
  return {
    left: `${rect.value.x}px`,
    top: `${rect.value.y}px`,
    width: `${rect.value.width}px`,
    height: `${rect.value.height}px`,
  }
})

const selectedClip = computed(() => recentClips.value.find((c) => c.id === selectedClipId.value) || null)

function selectClip(id: string) {
  selectedClipId.value = id
}
</script>

<template>
  <div class="vehicle-zone-picker">
    <div v-if="loading" class="muted-note">Loading recent frames…</div>
    <Message v-else-if="loadError" severity="error" :closable="false">Failed to load recent clips.</Message>
    <Message v-else-if="!recentClips.length" severity="warn" :closable="false">
      No clips yet for this camera — download a clip first, then come back to set the zone.
    </Message>
    <template v-else>
      <div class="thumb-strip">
        <button
          v-for="clip in recentClips"
          :key="clip.id"
          class="thumb-strip-item"
          :class="{ active: clip.id === selectedClipId }"
          type="button"
          @click="selectClip(clip.id)"
        >
          <img :src="clipThumbUrl(clip.id)" alt="" loading="lazy" />
        </button>
      </div>

      <div ref="containerEl" class="picker-canvas-wrap">
        <img
          v-if="selectedClip"
          :src="clipThumbUrl(selectedClip.id)"
          alt="Selected frame"
          class="picker-image"
          @load="onImageLoad"
        />
        <div class="picker-overlay" @pointerdown="onPointerDown" @pointermove="onPointerMove" @pointerup="onPointerUp">
          <div v-if="rectStyle" class="zone-rect" :style="rectStyle">
            <span class="zone-handle nw" />
            <span class="zone-handle ne" />
            <span class="zone-handle sw" />
            <span class="zone-handle se" />
          </div>
        </div>
      </div>

      <div class="picker-actions">
        <Button size="small" outlined severity="secondary" :disabled="!rect" @click="clearZone">Clear zone</Button>
        <span class="muted-note">Click and drag on the frame to draw or adjust the protected vehicle's zone.</span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.vehicle-zone-picker {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.thumb-strip {
  display: flex;
  gap: 0.4rem;
  overflow-x: auto;
  padding-bottom: 0.2rem;
}

.thumb-strip-item {
  flex: 0 0 auto;
  width: 64px;
  height: 48px;
  padding: 0;
  border-radius: 0.3rem;
  border: 2px solid transparent;
  overflow: hidden;
  cursor: pointer;
  background: var(--card2, rgba(255, 255, 255, 0.04));
}

.thumb-strip-item.active {
  border-color: var(--accent, #5b9cf6);
}

.thumb-strip-item img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.picker-canvas-wrap {
  position: relative;
  max-width: 480px;
  user-select: none;
}

.picker-image {
  width: 100%;
  display: block;
  border-radius: 0.4rem;
}

.picker-overlay {
  position: absolute;
  inset: 0;
  cursor: crosshair;
  touch-action: none;
}

.zone-rect {
  position: absolute;
  border: 2px solid var(--accent, #5b9cf6);
  background: rgba(91, 156, 246, 0.18);
  box-sizing: border-box;
}

.zone-handle {
  position: absolute;
  width: 10px;
  height: 10px;
  margin: -5px;
  border-radius: 50%;
  background: var(--accent, #5b9cf6);
  border: 1px solid #fff;
}

.zone-handle.nw {
  left: 0;
  top: 0;
}
.zone-handle.ne {
  left: 100%;
  top: 0;
}
.zone-handle.sw {
  left: 0;
  top: 100%;
}
.zone-handle.se {
  left: 100%;
  top: 100%;
}

.picker-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.muted-note {
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
