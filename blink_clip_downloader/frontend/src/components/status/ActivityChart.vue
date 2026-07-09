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
  const maxCount = Math.max(...dates.map((d) => byDate.get(d) ?? 0), 1)
  return dates.map((date) => {
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
      <div class="act-bar-wrap" :title="`${day.total} clips`" @click="emit('select-date', day.date)">
        <div class="act-bar" :style="{ width: `${day.pct.toFixed(1)}%` }"></div>
      </div>
      <span class="act-count">{{ day.total }}</span>
    </div>
  </div>
</template>
