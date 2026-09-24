<script setup lang="ts">
// Every zone on one camera's frame, drawn over the picture: a coloured
// outline per asset and its name. Positioned entirely in percentages over a
// 0-100 SVG viewBox (see assetZoneGeometry.ts), so it needs no measuring and
// fits whatever size the picture is shown at.
import { computed } from 'vue'
import { type OverlayZone, zoneLabelStyle, zoneSvgPoints } from './assetZoneGeometry'

const props = withDefaults(
  defineProps<{
    zones: OverlayZone[]
    /** The zone to draw attention to (a list row being hovered, say). */
    highlighted?: string | null
    /** Labels are buttons (preview) or plain text (under a drawing tool,
     * where they must not catch the pointer). */
    interactive?: boolean
    /** Drawn dashed and faint: the other assets while one is being drawn. */
    ghost?: boolean
  }>(),
  { highlighted: null, interactive: false, ghost: false },
)
const emit = defineEmits<{ select: [id: string]; hover: [id: string | null] }>()

const shapes = computed(() =>
  props.zones.map((z) => ({
    ...z,
    points: zoneSvgPoints(z.zone),
    label: zoneLabelStyle(z.zone),
    dim: Boolean(props.highlighted) && props.highlighted !== z.id,
  })),
)
</script>

<template>
  <div class="asset-zone-overlay" :class="{ ghost }">
    <svg class="zone-svg" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
      <polygon
        v-for="shape in shapes"
        :key="shape.id"
        :points="shape.points"
        class="zone-shape"
        :class="{ muted: shape.muted, dim: shape.dim, highlighted: highlighted === shape.id }"
        :style="{ '--zone-color': shape.color }"
        vector-effect="non-scaling-stroke"
      />
    </svg>
    <template v-for="shape in shapes" :key="`label-${shape.id}`">
      <button
        v-if="interactive"
        type="button"
        class="zone-label"
        :class="{ muted: shape.muted, dim: shape.dim }"
        :style="{ ...shape.label, '--zone-color': shape.color }"
        :aria-label="`Select ${shape.name}`"
        @click="emit('select', shape.id)"
        @mouseenter="emit('hover', shape.id)"
        @mouseleave="emit('hover', null)"
        @focus="emit('hover', shape.id)"
        @blur="emit('hover', null)"
      >
        {{ shape.name }}
      </button>
      <span
        v-else
        class="zone-label static"
        :class="{ muted: shape.muted }"
        :style="{ ...shape.label, '--zone-color': shape.color }"
        >{{ shape.name }}</span
      >
    </template>
  </div>
</template>

<style scoped>
.asset-zone-overlay {
  position: absolute;
  inset: 0;
  pointer-events: none;
}

/* An <svg> ignores inset:0 on its own — it needs an explicit size. */
.zone-svg {
  position: absolute;
  left: 0;
  top: 0;
  width: 100%;
  height: 100%;
  overflow: visible;
}

.zone-shape {
  fill: color-mix(in srgb, var(--zone-color) 20%, transparent);
  stroke: var(--zone-color);
  stroke-width: 2;
  stroke-linejoin: round;
  transition:
    opacity 0.15s var(--ease),
    stroke-width 0.15s var(--ease);
}

.zone-shape.highlighted {
  stroke-width: 3.5;
  fill: color-mix(in srgb, var(--zone-color) 32%, transparent);
}

.zone-shape.dim {
  opacity: 0.35;
}

.zone-shape.muted {
  stroke-dasharray: 5 4;
  fill: transparent;
  opacity: 0.6;
}

.ghost .zone-shape {
  stroke-dasharray: 5 4;
  fill: color-mix(in srgb, var(--zone-color) 10%, transparent);
  opacity: 0.75;
}

/* Over a photo, in either theme — so a fixed dark chip rather than a theme
   token, for the same reason the zone colours are fixed. */
.zone-label {
  position: absolute;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0.12rem 0.45rem;
  margin: 0;
  border: 0;
  border-left: 3px solid var(--zone-color);
  border-radius: 0.3rem;
  background: rgba(10, 10, 15, 0.8);
  color: #fff;
  font: inherit;
  font-size: 0.72rem;
  font-weight: 600;
  line-height: 1.4;
  pointer-events: auto;
  cursor: pointer;
}

.zone-label.static {
  pointer-events: none;
  cursor: default;
}

.zone-label:focus-visible {
  outline: 2px solid #fff;
  outline-offset: 1px;
}

.zone-label.dim {
  opacity: 0.55;
}

.zone-label.muted {
  opacity: 0.7;
  font-style: italic;
}

.ghost .zone-label {
  opacity: 0.8;
}
</style>
