<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getFeedbackStats } from '../../api/ai'
import type { FeedbackStats } from '../../api/types'

const stats = ref<FeedbackStats | null>(null)

const accuracyPct = computed(() => {
  if (!stats.value || !stats.value.total) return null
  return Math.round((stats.value.correct / stats.value.total) * 100)
})
const accuracyColor = computed(() => {
  const pct = accuracyPct.value
  if (pct == null) return 'var(--muted)'
  return pct >= 80 ? 'var(--success)' : pct >= 50 ? 'var(--warn)' : 'var(--danger)'
})

async function load() {
  try {
    stats.value = await getFeedbackStats()
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.error-only handling */
  }
}
onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">🧠 Adaptive Learning</h3>
    <div style="font-size: 0.85rem">
      <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.5rem; margin-bottom: 0.5rem">
        <div style="text-align: center">
          <div style="font-size: 1.4rem; font-weight: 700; color: var(--accent)">{{ stats?.total || 0 }}</div>
          <div style="font-size: 0.7rem; color: var(--muted)">Feedback given</div>
        </div>
        <div style="text-align: center">
          <div style="font-size: 1.4rem; font-weight: 700" :style="{ color: accuracyColor }">
            {{ accuracyPct == null ? '—' : `${accuracyPct}%` }}
          </div>
          <div style="font-size: 0.7rem; color: var(--muted)">Accuracy</div>
        </div>
      </div>
      <div style="color: var(--muted); font-size: 0.78rem">
        <template v-if="stats && stats.total > 0">
          {{ stats.false_positive || 0 }} false positive(s), {{ stats.false_negative || 0 }} false negative(s) reported
        </template>
        <template v-else>No feedback recorded yet.</template>
      </div>
      <p style="font-size: 0.72rem; color: var(--muted); margin-top: 0.5rem">
        Mark verdicts 👍/👎 on the Suspicious Activity Feed below, or in a clip's AI panel, to auto-tune per-camera alert
        thresholds and teach future analyses from your corrections.
      </p>
    </div>
  </div>
</template>
