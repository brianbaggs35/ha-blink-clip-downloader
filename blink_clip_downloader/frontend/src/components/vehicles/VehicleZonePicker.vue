<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import SelectButton from 'primevue/selectbutton'
import { clipThumbUrl, listClips } from '../../api/clips'
import { clearVehicleZone, saveVehicleZone, vehicleZoneSnapshotUrl } from '../../api/vehicle'
import type { CarZone, ClipListItem } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import {
  type CornerHandle,
  type Point,
  type Rect,
  MIN_RECT_SIZE,
  addFreeformPoint,
  clampRect,
  fractionToPolygonPoints,
  fractionToRect,
  hitTest,
  moveRect,
  pointsToSvgAttr,
  polygonToFraction,
  rectFromPoints,
  rectToFraction,
  resizeRect,
} from './vehicleZoneGeometry'

const props = defineProps<{ camera: string; modelValue: CarZone | null }>()
const emit = defineEmits<{ 'update:modelValue': [CarZone | null] }>()

const confirm = useConfirm()
const toast = useToastStore()

// The component's own source of truth for "what's currently saved",
// initialised from the prop but updated locally the moment a save/clear
// succeeds — the preview UI must not wait on the parent round-tripping a
// new modelValue prop back down before it can render correctly.
const savedZone = ref<CarZone | null>(props.modelValue)

// preview: shows the persisted reference snapshot with the saved zone drawn
// on top, frozen and non-interactive — revisiting this tab later never
// silently shows a different frame than the one the zone was actually drawn
// on, even if a newer clip (with something else in it) has since come in.
// edit: the interactive picker (thumb strip + drawing canvas), entered
// explicitly; nothing is persisted until "Save zone" is clicked.
const mode = ref<'preview' | 'edit'>(savedZone.value ? 'preview' : 'edit')
// Cache-busts the snapshot <img> after a fresh save so the browser can't
// keep showing a stale cached image at the same, camera-stable URL.
const snapshotVersion = ref(0)

const loading = ref(true)
const loadError = ref(false)
const saving = ref(false)
const recentClips = ref<ClipListItem[]>([])
const selectedClipId = ref('')

const containerEl = ref<HTMLDivElement | null>(null)
const containerSize = ref({ width: 0, height: 0 })

const drawShape = ref<'rect' | 'polygon'>('rect')
const shapeOptions = [
  { label: '▭ Rectangle', value: 'rect' as const },
  { label: '✏️ Freeform', value: 'polygon' as const },
]

const rect = ref<Rect | null>(null)
const freeformPath = ref<Point[]>([])
const isDrawingFreeform = ref(false)

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

function resetDraft() {
  rect.value = null
  freeformPath.value = []
  drawShape.value = 'rect'
  containerSize.value = { width: 0, height: 0 }
}

function enterEditMode() {
  resetDraft()
  mode.value = 'edit'
  if (!recentClips.value.length) void loadRecentClips()
}

onMounted(() => {
  if (mode.value === 'edit') void loadRecentClips()
  else loading.value = false
})

// A single combined watcher (rather than two separate ones on props.camera
// and props.modelValue) — both often change in the same tick when a parent
// swaps to a different camera, and two independent watchers give no
// guarantee about which runs first, risking mode being (re-)computed
// against a not-yet-updated savedZone.
watch(
  () => [props.camera, props.modelValue] as const,
  ([newCamera, newModelValue], [oldCamera]) => {
    savedZone.value = newModelValue
    if (newCamera === oldCamera) return
    resetDraft()
    mode.value = savedZone.value ? 'preview' : 'edit'
    if (mode.value === 'edit') void loadRecentClips()
  },
)

function measureContainer() {
  if (!containerEl.value) return
  const box = containerEl.value.getBoundingClientRect()
  containerSize.value = { width: box.width, height: box.height }
}

async function onImageLoad() {
  await nextTick()
  measureContainer()
}

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
  if (drawShape.value === 'polygon') {
    isDrawingFreeform.value = true
    freeformPath.value = [[x, y]]
    ;(e.target as Element).setPointerCapture?.(e.pointerId)
    return
  }
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
  const { x, y } = pointerPos(e)
  if (isDrawingFreeform.value) {
    freeformPath.value = addFreeformPoint(freeformPath.value, [x, y])
    return
  }
  if (!drag.value) return
  const { width, height } = containerSize.value
  if (drag.value.kind === 'draw') {
    rect.value = clampRect(rectFromPoints(drag.value.startX, drag.value.startY, x, y), width, height)
  } else if (drag.value.kind === 'resize' && rect.value) {
    rect.value = clampRect(resizeRect(rect.value, drag.value.handle!, x, y), width, height)
  } else if (drag.value.kind === 'move' && drag.value.origRect) {
    rect.value = moveRect(drag.value.origRect, x - drag.value.startX, y - drag.value.startY, width, height)
  }
}

function onPointerUp() {
  if (isDrawingFreeform.value) {
    // Auto-close: the path already renders as a closed <polygon> regardless
    // of exactly where the pointer was released, matching a real lasso
    // tool — the user never has to trace precisely back to the start point.
    isDrawingFreeform.value = false
    return
  }
  drag.value = null
}

function switchShape(next: 'rect' | 'polygon') {
  drawShape.value = next
  rect.value = null
  freeformPath.value = []
}

const hasDraft = computed(() => {
  if (drawShape.value === 'polygon') return freeformPath.value.length >= 3
  return !!rect.value && rect.value.width >= MIN_RECT_SIZE && rect.value.height >= MIN_RECT_SIZE
})

async function saveZone() {
  const { width, height } = containerSize.value
  const zone: CarZone | null =
    drawShape.value === 'polygon'
      ? polygonToFraction(freeformPath.value, width, height)
      : rect.value
        ? rectToFraction(rect.value, width, height)
        : null
  if (!zone || !selectedClipId.value) return

  saving.value = true
  try {
    const result = await saveVehicleZone(props.camera, zone, selectedClipId.value)
    savedZone.value = result.car_zone
    emit('update:modelValue', result.car_zone)
    snapshotVersion.value++
    resetDraft()
    mode.value = 'preview'
    toast.show('Vehicle zone saved')
  } catch {
    toast.show('Failed to save vehicle zone', true)
  } finally {
    saving.value = false
  }
}

function cancelEdit() {
  resetDraft()
  if (savedZone.value) mode.value = 'preview'
}

// Resets just the in-progress shape (not drawShape/selectedClipId) so a
// messy freeform trace or a badly-placed rectangle can be wiped and
// immediately redrawn on the same frame, without falling back to "Cancel"
// (which only exists once a zone is already saved) or starting a new drag
// gesture just to implicitly clear the old one.
function clearDraft() {
  rect.value = null
  freeformPath.value = []
}

async function clearZone() {
  const ok = await confirm(
    `Clear the protected-vehicle zone for "${props.camera}"? This removes the saved zone and its reference image.`,
    'Clear vehicle zone?',
  )
  if (!ok) return
  saving.value = true
  try {
    await clearVehicleZone(props.camera)
    savedZone.value = null
    emit('update:modelValue', null)
    resetDraft()
    mode.value = 'edit'
    if (!recentClips.value.length) void loadRecentClips()
    toast.show('Vehicle zone cleared')
  } catch {
    toast.show('Failed to clear vehicle zone', true)
  } finally {
    saving.value = false
  }
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

const freeformPointsAttr = computed(() => pointsToSvgAttr(freeformPath.value))

const selectedClip = computed(() => recentClips.value.find((c) => c.id === selectedClipId.value) || null)

function selectClip(id: string) {
  selectedClipId.value = id
  rect.value = null
  freeformPath.value = []
}

const previewSnapshotUrl = computed(() => `${vehicleZoneSnapshotUrl(props.camera)}?v=${snapshotVersion.value}`)

const previewRectStyle = computed(() => {
  const zone = savedZone.value
  const { width, height } = containerSize.value
  if (!zone || zone.shape !== 'rect' || !width || !height) return null
  const r = fractionToRect(zone, width, height)
  return { left: `${r.x}px`, top: `${r.y}px`, width: `${r.width}px`, height: `${r.height}px` }
})

const previewPolygonAttr = computed(() => {
  const zone = savedZone.value
  const { width, height } = containerSize.value
  if (!zone || zone.shape !== 'polygon' || !width || !height) return ''
  return pointsToSvgAttr(fractionToPolygonPoints(zone, width, height))
})
</script>

<template>
  <div class="vehicle-zone-picker">
    <template v-if="mode === 'preview' && savedZone">
      <div ref="containerEl" class="picker-canvas-wrap">
        <img
          :src="previewSnapshotUrl"
          alt="Saved protected-vehicle zone reference frame"
          class="picker-image"
          @load="onImageLoad"
        />
        <div class="picker-overlay picker-overlay-static">
          <div v-if="previewRectStyle" class="zone-rect" :style="previewRectStyle" />
          <svg v-if="previewPolygonAttr" class="zone-svg">
            <polygon :points="previewPolygonAttr" class="zone-polygon" />
          </svg>
        </div>
      </div>
      <div class="picker-actions">
        <Button size="small" outlined @click="enterEditMode">Edit zone</Button>
        <Button size="small" outlined severity="danger" :disabled="saving" @click="clearZone">Clear zone</Button>
      </div>
    </template>

    <template v-else>
      <Message v-if="!savedZone" severity="info" size="small" :closable="false" class="empty-state-msg">
        No vehicle selected. Select your vehicle in a frame below and click save to set a vehicle.
      </Message>

      <div v-if="loading" class="muted-note">Loading recent frames…</div>
      <Message v-else-if="loadError" severity="error" size="small" :closable="false"
        >Failed to load recent clips.</Message
      >
      <Message v-else-if="!recentClips.length" severity="warn" size="small" :closable="false">
        No clips yet for this camera — download a clip first, then come back to set the zone.
      </Message>
      <template v-else>
        <SelectButton
          :model-value="drawShape"
          :options="shapeOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          class="shape-toggle"
          aria-label="Zone drawing tool"
          @update:model-value="switchShape"
        />

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
          <div
            class="picker-overlay"
            :class="{ lasso: drawShape === 'polygon' }"
            @pointerdown="onPointerDown"
            @pointermove="onPointerMove"
            @pointerup="onPointerUp"
          >
            <div v-if="drawShape === 'rect' && rectStyle" class="zone-rect" :style="rectStyle">
              <span class="zone-handle nw" />
              <span class="zone-handle ne" />
              <span class="zone-handle sw" />
              <span class="zone-handle se" />
            </div>
            <svg v-if="drawShape === 'polygon' && freeformPath.length > 1" class="zone-svg">
              <polygon :points="freeformPointsAttr" class="zone-polygon" />
            </svg>
          </div>
        </div>

        <div class="picker-actions">
          <Button size="small" :disabled="!hasDraft || saving" :loading="saving" @click="saveZone">
            {{ saving ? 'Saving…' : 'Save zone' }}
          </Button>
          <Button v-if="hasDraft" size="small" outlined severity="secondary" :disabled="saving" @click="clearDraft"
            >Clear</Button
          >
          <Button v-if="savedZone" size="small" outlined severity="secondary" :disabled="saving" @click="cancelEdit"
            >Cancel</Button
          >
          <span class="muted-note">{{
            drawShape === 'rect'
              ? "Click and drag on the frame to draw or adjust the protected vehicle's zone."
              : "Click, hold, and trace around the vehicle's outline — release to close the shape."
          }}</span>
        </div>
      </template>
    </template>
  </div>
</template>

<style scoped>
.vehicle-zone-picker {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.shape-toggle {
  align-self: flex-start;
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

.picker-overlay-static {
  cursor: default;
  pointer-events: none;
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

.zone-svg {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  overflow: visible;
}

.zone-polygon {
  fill: rgba(91, 156, 246, 0.18);
  stroke: var(--accent, #5b9cf6);
  stroke-width: 2;
  stroke-linejoin: round;
}

.picker-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.empty-state-msg {
  margin-bottom: 0.2rem;
}

.muted-note {
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
