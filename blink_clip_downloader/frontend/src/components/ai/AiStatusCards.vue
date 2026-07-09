<script setup lang="ts">
import { computed } from 'vue'
import type { AiStatus } from '../../api/types'

const props = defineProps<{ status: AiStatus }>()

const scheduleText = computed(() => {
  const q = props.status.queue
  if (!q) return 'Loading…'
  if (q.schedule_start && q.schedule_end) {
    return { time: `${q.schedule_start} – ${q.schedule_end}`, active: q.in_schedule }
  }
  return 'Always active (no schedule set)'
})

const lastAnalysis = computed(() => {
  const last = props.status.analysis_stats.last_analysis
  return last ? new Date(last).toLocaleString() : '—'
})
</script>

<template>
  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">Schedule</h3>
    <div style="font-size: 0.85rem; color: var(--muted)">
      <template v-if="typeof scheduleText === 'string'">{{ scheduleText }}</template>
      <template v-else>
        {{ scheduleText.time }}<br />
        {{ scheduleText.active ? '🟢 Active' : '🔴 Waiting' }}
      </template>
    </div>
  </div>

  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">Queue Status</h3>
    <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem">
      <div style="text-align: center">
        <div style="font-size: 1.5rem; font-weight: 700; color: var(--accent)">{{ status.queue?.pending || 0 }}</div>
        <div style="font-size: 0.72rem; color: var(--muted)">Pending</div>
      </div>
      <div style="text-align: center">
        <div style="font-size: 1.5rem; font-weight: 700; color: var(--warn)">{{ status.queue?.processing || 0 }}</div>
        <div style="font-size: 0.72rem; color: var(--muted)">Processing</div>
      </div>
      <div style="text-align: center">
        <div style="font-size: 1.5rem; font-weight: 700; color: var(--success)">{{ status.queue?.completed || 0 }}</div>
        <div style="font-size: 0.72rem; color: var(--muted)">Completed</div>
      </div>
      <div style="text-align: center">
        <div style="font-size: 1.5rem; font-weight: 700; color: var(--danger)">{{ status.queue?.failed || 0 }}</div>
        <div style="font-size: 0.72rem; color: var(--muted)">Failed</div>
      </div>
    </div>
  </div>

  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">Analysis Stats</h3>
    <div style="font-size: 0.85rem">
      <div>Total Analyzed: <strong>{{ status.analysis_stats.total_analyzed || 0 }}</strong></div>
      <div>Suspicious: <strong style="color: var(--danger)">{{ status.analysis_stats.suspicious_count || 0 }}</strong></div>
      <div style="color: var(--muted); font-size: 0.78rem; margin-top: 0.4rem">Last: <span>{{ lastAnalysis }}</span></div>
    </div>
  </div>
</template>
