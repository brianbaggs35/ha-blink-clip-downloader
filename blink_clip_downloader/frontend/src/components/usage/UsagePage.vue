<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { clearAiUsage, getAiUsage } from '../../api/ai'
import { fmtCost, fmtNum, providerLabel } from '../../api/constants'
import type { AiUsage } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import ProviderNote from './ProviderNote.vue'

const toast = useToastStore()
const confirm = useConfirm()

const usage = ref<AiUsage | null>(null)
const loading = ref(true)

const TOKEN_PROVIDERS = new Set(['ollama', 'ollama_cloud', 'anthropic', 'openai', 'moondream_cloud'])
const showTokens = computed(() => TOKEN_PROVIDERS.has(usage.value?.provider || ''))

const showEscalationNote = computed(() => (usage.value?.total_escalations || 0) > 0)

const showCost = computed(() => usage.value?.total_estimated_cost != null && (usage.value?.total_tokens || 0) > 0)
const showDisabledMsg = computed(() => !usage.value?.enabled && (usage.value?.total_analyses || 0) === 0)

// Two derived stats filling out the row the "Estimated Cost" tile vacated
// (moved up next to Total Tokens) — both computed client-side from fields
// the usage payload already carries, no new backend data needed.
const avgCostPerClip = computed(() => {
  const total = usage.value?.total_estimated_cost
  const count = usage.value?.total_analyses || 0
  if (!showCost.value || total == null || count <= 0) return null
  return total / count
})
const modelsUsedCount = computed(() => new Set((usage.value?.by_model || []).map((m) => m.model).filter(Boolean)).size)

async function load() {
  try {
    usage.value = await getAiUsage()
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.error-only handling */
  } finally {
    loading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval>
onMounted(() => {
  void load()
  pollTimer = setInterval(load, 10_000)
})
onUnmounted(() => clearInterval(pollTimer))

async function clearUsage() {
  if (
    !(await confirm(
      'Clear all AI usage stats (tokens, cost, escalations)? Per-clip analysis results are not affected.',
      'Clear AI usage stats?',
    ))
  )
    return
  try {
    await clearAiUsage()
    toast.show('AI usage stats cleared')
    await load()
  } catch {
    toast.show('Failed to clear usage stats', true)
  }
}
</script>

<template>
  <div class="auto-content">
    <h2 style="display: flex; align-items: center; justify-content: space-between; gap: 1rem">
      <span>AI Token Usage</span>
      <button type="button" class="btn sm danger" @click="clearUsage">🗑 Clear Stats</button>
    </h2>

    <div v-if="loading" style="padding: 2rem"><LoadingIndicator /></div>
    <template v-else-if="usage">
      <div v-if="showDisabledMsg" class="status-card" style="padding: 2rem; text-align: center; color: var(--muted)">
        <p style="font-size: 1.2rem; margin-bottom: 0.8rem">📊 No AI Usage Data</p>
        <p>Enable AI analysis in the add-on settings. Usage statistics will appear after the first analysis run.</p>
      </div>
      <div v-else>
        <div class="usage-grid">
          <div class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_analyses) }}</div>
            <div class="lbl">Clips Analyzed</div>
          </div>
          <div v-if="showTokens" class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_tokens) }}</div>
            <div class="lbl">Total Tokens</div>
          </div>
          <div v-if="showCost" class="usage-stat">
            <div class="num">{{ fmtCost(usage.total_estimated_cost) }}</div>
            <div class="lbl">Estimated Cost</div>
          </div>
          <div v-if="showTokens" class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_tokens_completion) }}</div>
            <div class="lbl">Completion Tokens</div>
          </div>
          <div v-if="usage.total_escalations > 0" class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_escalations) }}</div>
            <div class="lbl">Escalations</div>
          </div>
          <div v-if="usage.total_escalations > 0" class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_escalation_tokens) }}</div>
            <div class="lbl">Escalation Tokens</div>
          </div>
          <div v-if="showTokens" class="usage-stat">
            <div class="num">{{ fmtNum(usage.total_tokens_prompt) }}</div>
            <div class="lbl">Prompt Tokens</div>
          </div>
          <div v-if="avgCostPerClip != null" class="usage-stat">
            <div class="num">{{ fmtCost(avgCostPerClip) }}</div>
            <div class="lbl">Avg. Cost / Clip</div>
          </div>
          <div v-if="modelsUsedCount > 0" class="usage-stat">
            <div class="num">{{ fmtNum(modelsUsedCount) }}</div>
            <div class="lbl">Models Used</div>
          </div>
        </div>

        <div class="status-card" style="margin-bottom: 1.2rem">
          <h3 style="margin-bottom: 0.75rem">Current Provider</h3>
          <div class="status-row">
            <span class="lbl">Provider</span><span class="val">{{ providerLabel(usage.provider) }}</span>
          </div>
          <div class="status-row">
            <span class="lbl">Model</span><span class="val">{{ usage.model || '—' }}</span>
          </div>
          <ProviderNote :provider="usage.provider" :show-escalation-note="showEscalationNote" />
        </div>

        <h3 style="margin-bottom: 0.6rem">Per-Model Breakdown</h3>
        <div class="table-scroll">
          <table class="usage-table">
            <thead>
              <tr>
                <th>Model</th>
                <th style="text-align: right">Analyses</th>
                <th style="text-align: right">Prompt Tokens</th>
                <th style="text-align: right">Completion Tokens</th>
                <th style="text-align: right">Total Tokens</th>
                <th style="text-align: right">Est. Cost</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(m, i) in usage.by_model" :key="i">
                <td>
                  {{ m.model || '—'
                  }}<span v-if="m.escalated" style="color: var(--muted); font-size: 0.75em"> (escalated)</span>
                </td>
                <td style="text-align: right">{{ fmtNum(m.analyses || 0) }}</td>
                <template v-if="showTokens">
                  <td style="text-align: right">{{ fmtNum(m.tokens_prompt || 0) }}</td>
                  <td style="text-align: right">{{ fmtNum(m.tokens_completion || 0) }}</td>
                  <td style="text-align: right">{{ fmtNum((m.tokens_prompt || 0) + (m.tokens_completion || 0)) }}</td>
                </template>
                <template v-else>
                  <td style="text-align: right; color: var(--muted)">N/A</td>
                  <td style="text-align: right; color: var(--muted)">N/A</td>
                  <td style="text-align: right; color: var(--muted)">N/A</td>
                </template>
                <td style="text-align: right">{{ fmtCost(m.cost) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="!usage.by_model.length" style="color: var(--muted); padding: 1rem; text-align: center">
            No analysis data yet. Run the AI analysis to see usage statistics.
          </p>
        </div>

        <h3 style="margin-bottom: 0.6rem">Daily Usage (Last 14 Days)</h3>
        <div class="table-scroll">
          <table class="usage-table">
            <thead>
              <tr>
                <th>Date</th>
                <th style="text-align: right">Analyses</th>
                <th style="text-align: right">Total Tokens</th>
                <th style="text-align: right">Est. Cost</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in usage.daily" :key="row.day">
                <td>{{ row.day || '—' }}</td>
                <td style="text-align: right">{{ fmtNum(row.analyses || 0) }}</td>
                <td style="text-align: right">{{ fmtNum(row.tokens_total || 0) }}</td>
                <td style="text-align: right">{{ fmtCost(row.cost) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-if="!usage.daily.length" style="color: var(--muted); padding: 1rem; text-align: center">
            No analysis activity in the last 14 days.
          </p>
        </div>
      </div>
    </template>
  </div>
</template>
