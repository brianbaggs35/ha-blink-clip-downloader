<script setup lang="ts">
import { computed } from 'vue'
import type { ActivityRow } from '../../api/types'

const props = defineProps<{ rows: ActivityRow[] }>()
const emit = defineEmits<{ 'select-date': [date: string] }>()

interface DayBar {
  date: string
  label: string
  total: number
  pct: number
}

const days = computed<DayBar[]>(() => {
  const byDate = new Map<string, number>()
  for (const { date, count } of props.rows) {
    byDate.set(date, (byDate.get(date) ?? 0) + count)
  }
  const dates = [...byDate.keys()].sort().reverse()
  // Every `d`/`date` below comes from byDate's own keys(), so .get() always
  // returns the defined count that was just set for it -- the `?? 0`
  // fallbacks are unreachable in practice, kept only for type narrowing
  // (Map#get's return type is always `T | undefined`).
  /* v8 ignore next */
  /* istanbul ignore next -- see v8-ignore comment above; same dead branch */
  const maxCount = Math.max(...dates.map((d) => byDate.get(d) ?? 0), 1)
  return dates.map((date) => {
    /* v8 ignore next */
    /* istanbul ignore next -- see v8-ignore comment above; same dead branch */
    const total = byDate.get(date) ?? 0
    const label = new Date(`${date}T12:00:00`).toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
    })
    return { date, label, total, pct: (total / maxCount) * 100 }
  })
})
</script>

<template>
  <p v-if="!days.length" style="color: var(--muted); font-size: 0.84rem">No recent activity.</p>
  <div v-else id="act-chart">
    <div v-for="day in days" :key="day.date" class="act-row">
      <span class="act-date">{{ day.label }}</span>
      <!-- A real button, not a div with a click handler: this filters the
           Library to that day, and as a div it was unreachable by keyboard
           entirely. Being a button brings focus, Enter/Space and the right
           role with it, rather than bolting tabindex/role/@keydown on. -->
      <button
        type="button"
        class="act-bar-wrap"
        :aria-label="`Show the ${day.total} clip(s) from ${day.label}`"
        :title="`${day.total} clips`"
        @click="emit('select-date', day.date)"
      >
        <span class="act-bar" :style="{ width: `${day.pct.toFixed(1)}%` }"></span>
      </button>
      <span class="act-count">{{ day.total }}</span>
    </div>
  </div>
</template>
