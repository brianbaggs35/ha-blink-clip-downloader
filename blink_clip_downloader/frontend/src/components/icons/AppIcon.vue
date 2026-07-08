<script setup lang="ts">
import { computed } from 'vue'
import { ICONS, type IconName } from './paths'

const props = defineProps<{ name: IconName }>()
const def = computed(() => ICONS[props.name])
</script>

<template>
  <svg class="icon" :class="{ filled: def.filled }" viewBox="0 0 24 24">
    <rect
      v-for="(r, i) in def.rects"
      :key="'r' + i"
      :x="r.x"
      :y="r.y"
      :width="r.width"
      :height="r.height"
      :rx="r.rx ?? 0"
    />
    <circle v-for="(c, i) in def.circles" :key="'c' + i" :cx="c.cx" :cy="c.cy" :r="c.r" />
    <path v-for="(p, i) in def.paths" :key="'p' + i" :d="p" />
  </svg>
</template>
