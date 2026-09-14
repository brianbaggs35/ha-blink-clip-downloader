<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { getClipDetections } from '../../api/ai'
import type { DetectedBox } from '../../api/types'

const props = defineProps<{ clipId: string; currentTime: number }>()

const objects = ref<DetectedBox[]>([])

/** Which broad kind a label is, for colouring. Deliberately three buckets
 *  rather than one colour per COCO class: the point of the overlay is to
 *  show at a glance where the person and the vehicle were, not to be a
 *  legend. */
function kind(label: string): string {
  if (label === 'person') return 'person'
  if (['car', 'truck', 'bus', 'motorcycle', 'bicycle'].includes(label)) return 'vehicle'
  return 'other'
}

async function load() {
  try {
    objects.value = (await getClipDetections(props.clipId)).objects ?? []
  } catch {
    // Silent: the overlay is an extra, and a modal that pops an error
    // because an optional decoration failed is worse than one without it.
    objects.value = []
  }
}

watch(() => props.clipId, load, { immediate: true })

/** Detections are sampled seconds apart, so a box "belongs" to the window
 *  around its own timestamp rather than to a single instant — otherwise the
 *  overlay would be blank for almost the whole clip. Derived from the data
 *  rather than assumed, since the sampling interval is a setting. */
const halfWindow = computed(() => {
  const offsets = [...new Set(objects.value.map((o) => o.offset_seconds))].sort((a, b) => a - b)
  const gaps = offsets.slice(1).map((offset, i) => offset - offsets[i])
  const smallest = gaps.length ? Math.min(...gaps) : 0
  return (smallest > 0 ? smallest : 2) / 2
})

const visible = computed(() =>
  objects.value.filter((o) => Math.abs(o.offset_seconds - props.currentTime) <= halfWindow.value),
)
</script>

<template>
  <svg
    v-if="visible.length"
    class="detection-overlay"
    viewBox="0 0 100 100"
    preserveAspectRatio="none"
    aria-hidden="true"
    data-testid="detection-overlay"
  >
    <g v-for="(obj, i) in visible" :key="`${obj.label}-${obj.track_id}-${i}`">
      <rect
        :class="['detection-box', `detection-${kind(obj.label)}`]"
        :x="obj.box[0] * 100"
        :y="obj.box[1] * 100"
        :width="Math.max(0, (obj.box[2] - obj.box[0]) * 100)"
        :height="Math.max(0, (obj.box[3] - obj.box[1]) * 100)"
      />
    </g>
  </svg>
  <div v-if="visible.length" class="detection-labels" aria-hidden="true">
    <span
      v-for="(obj, i) in visible"
      :key="`label-${obj.label}-${obj.track_id}-${i}`"
      :class="['detection-label', `detection-${kind(obj.label)}`]"
      :style="{ left: `${obj.box[0] * 100}%`, top: `${obj.box[1] * 100}%` }"
    >
      {{ obj.label }}
    </span>
  </div>
</template>

<style scoped>
/* Both layers sit over the player and must never swallow a click meant for
   it — pausing by clicking the video has to keep working with the overlay
   on. */
.detection-overlay,
.detection-labels {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 2;
}
.detection-box {
  fill: none;
  /* vector-effect keeps the stroke one pixel wide despite the
     non-uniform viewBox scaling, which would otherwise stretch it into a
     wedge on any non-square video. */
  vector-effect: non-scaling-stroke;
  stroke-width: 2;
}
.detection-box.detection-person {
  stroke: #ffd166;
}
.detection-box.detection-vehicle {
  stroke: #4dd4ac;
}
.detection-box.detection-other {
  stroke: #9aa7b8;
}
.detection-label {
  position: absolute;
  transform: translateY(-100%);
  padding: 0 4px;
  font-size: 0.7rem;
  line-height: 1.35;
  border-radius: 3px 3px 0 0;
  color: #11161d;
  white-space: nowrap;
}
.detection-label.detection-person {
  background: #ffd166;
}
.detection-label.detection-vehicle {
  background: #4dd4ac;
}
.detection-label.detection-other {
  background: #9aa7b8;
}
</style>
