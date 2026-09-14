<script setup lang="ts">
import { computed, ref } from 'vue'
import Card from 'primevue/card'
import type { AiStatus } from '../../api/types'
import AnalysisFailuresModal from './AnalysisFailuresModal.vue'

const props = defineProps<{ status: AiStatus }>()
const showFailures = ref(false)

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
  <Card>
    <template #title><h3 style="margin: 0; font: inherit; color: inherit">Schedule</h3></template>
    <template #content>
      <div style="font-size: 0.85rem; color: var(--muted)">
        <template v-if="typeof scheduleText === 'string'">{{ scheduleText }}</template>
        <template v-else>
          {{ scheduleText.time }}<br />
          {{ scheduleText.active ? '🟢 Active' : '🔴 Waiting' }}
        </template>
      </div>
    </template>
  </Card>

  <Card>
    <template #title><h3 style="margin: 0; font: inherit; color: inherit">Queue Status</h3></template>
    <template #content>
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem">
        <div style="text-align: center">
          <div style="font-size: 1.5rem; font-weight: 700; color: var(--accent)">{{ status.queue?.pending || 0 }}</div>
          <div style="font-size: 0.72rem; color: var(--muted)">Pending</div>
        </div>
        <div style="text-align: center">
          <div style="font-size: 1.5rem; font-weight: 700; color: var(--warn)">
            {{ status.queue?.processing || 0 }}
          </div>
          <div style="font-size: 0.72rem; color: var(--muted)">Processing</div>
        </div>
        <div style="text-align: center">
          <div style="font-size: 1.5rem; font-weight: 700; color: var(--success)">
            {{ status.queue?.completed || 0 }}
          </div>
          <div style="font-size: 0.72rem; color: var(--muted)">Completed</div>
        </div>
        <div style="text-align: center">
          <button
            type="button"
            class="failed-stat-btn"
            :disabled="!status.queue?.failed"
            :aria-label="`View details for ${status.queue?.failed || 0} failed analyses`"
            @click="showFailures = true"
          >
            <div style="font-size: 1.5rem; font-weight: 700; color: var(--danger)">
              {{ status.queue?.failed || 0 }}
            </div>
            <div style="font-size: 0.72rem; color: var(--muted)">Failed</div>
          </button>
        </div>
      </div>
    </template>
  </Card>

  <Card>
    <template #title><h3 style="margin: 0; font: inherit; color: inherit">Analysis Stats</h3></template>
    <template #content>
      <div style="font-size: 0.85rem">
        <div>
          Total Analyzed: <strong>{{ status.analysis_stats.total_analyzed || 0 }}</strong>
        </div>
        <div>
          Suspicious: <strong style="color: var(--danger)">{{ status.analysis_stats.suspicious_count || 0 }}</strong>
        </div>
        <div style="color: var(--muted); font-size: 0.78rem; margin-top: 0.4rem">
          Last: <span>{{ lastAnalysis }}</span>
        </div>
      </div>
    </template>
  </Card>

  <AnalysisFailuresModal v-if="showFailures" @close="showFailures = false" />
</template>

<style scoped>
.failed-stat-btn {
  width: 100%;
  background: none;
  border: none;
  padding: 0.15rem 0.3rem;
  margin: 0;
  font: inherit;
  border-radius: 6px;
  cursor: pointer;
}

.failed-stat-btn:disabled {
  cursor: default;
}

.failed-stat-btn:not(:disabled):hover {
  background: var(--card-hover);
}

.failed-stat-btn:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
</style>
