<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import SelectButton from 'primevue/selectbutton'
import { clearAiUsage, getAiUsage, getAiUsagePeriods } from '../../api/ai'
import { fmtCost, fmtNum, providerLabel } from '../../api/constants'
import type { AiUsage, AiUsagePeriods, PeriodUsageWire } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import EmptyState from '../layout/EmptyState.vue'
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
// Guards against a malformed/incomplete API response the same way the rest
// of this page already treats missing fields (see showCost/showTokens
// above) — by_model/daily are always real arrays in a well-formed
// AiUsage response, but nothing upstream actually guarantees that at
// runtime, and an unguarded usage.by_model.length below would otherwise
// throw instead of just rendering the empty-state message.
const byModel = computed(() => usage.value?.by_model || [])
const dailyRows = computed(() => usage.value?.daily || [])

// Usage history granularity. Day is free — it reads the `daily` array the
// 10s usage poll already delivers. Week and Month come from their own
// endpoint, which is deliberately NOT part of that poll: it aggregates a
// year of rows, and no one who never opens this view should pay for that on
// every tick. So it is fetched on first switch away from Day, cached here,
// and thereafter refreshed by the poll only while it is actually on screen.
type Granularity = 'day' | 'week' | 'month'
const GRANULARITY_OPTIONS: { label: string; value: Granularity }[] = [
  { label: 'Day', value: 'day' },
  { label: 'Week', value: 'week' },
  { label: 'Month', value: 'month' },
]
const granularity = ref<Granularity>('day')
const periods = ref<AiUsagePeriods | null>(null)
const periodsLoading = ref(false)
const periodsFailed = ref(false)

const HISTORY_META: Record<Granularity, { heading: string; column: string; empty: string }> = {
  day: { heading: 'Last 14 Days', column: 'Date', empty: 'No analysis activity in the last 14 days.' },
  week: { heading: 'Last 12 Weeks', column: 'Week', empty: 'No analysis activity in the last 12 weeks.' },
  month: { heading: 'Last 12 Months', column: 'Month', empty: 'No analysis activity in the last 12 months.' },
}
const historyMeta = computed(() => HISTORY_META[granularity.value])

// One row shape for all three views, so the table markup stays single-source.
const historyRows = computed<{ label: string; analyses: number; tokens: number; cost: number | null }[]>(() => {
  if (granularity.value === 'day') {
    return dailyRows.value.map((r) => ({
      label: r.day || '—',
      analyses: r.analyses || 0,
      tokens: r.tokens_total || 0,
      cost: r.cost,
    }))
  }
  const rows: PeriodUsageWire[] = (granularity.value === 'week' ? periods.value?.weekly : periods.value?.monthly) || []
  return rows.map((r) => ({
    label: formatPeriod(r.period),
    analyses: r.analyses || 0,
    tokens: r.tokens_total || 0,
    cost: r.cost,
  }))
})

// Totals for the selected view — the actual question this feature answers
// ("what am I spending a week/month"), so it should not require the reader to
// add the column up themselves. Null cost across every row stays null rather
// than becoming $0.00, matching how the per-row cost is treated.
const historyTotals = computed(() => {
  const rows = historyRows.value
  const priced = rows.filter((r) => r.cost != null)
  return {
    analyses: rows.reduce((sum, r) => sum + r.analyses, 0),
    tokens: rows.reduce((sum, r) => sum + r.tokens, 0),
    cost: priced.length ? priced.reduce((sum, r) => sum + (r.cost as number), 0) : null,
  }
})

const MONTH_NAMES = [
  'January',
  'February',
  'March',
  'April',
  'May',
  'June',
  'July',
  'August',
  'September',
  'October',
  'November',
  'December',
]

/** Turn a wire bucket key into something readable: `2026-02` -> `February
 *  2026`, `2026-W07` -> `Week 07, 2026`. An unrecognised key is passed
 *  through untouched rather than mangled. */
function formatPeriod(key: string): string {
  const week = /^(\d{4})-W(\d{2})$/.exec(key)
  if (week) return `Week ${week[2]}, ${week[1]}`
  const month = /^(\d{4})-(\d{2})$/.exec(key)
  if (month) {
    const name = MONTH_NAMES[Number(month[2]) - 1]
    if (name) return `${name} ${month[1]}`
  }
  return key || '—'
}
const modelsUsedCount = computed(() => new Set(byModel.value.map((m) => m.model).filter(Boolean)).size)

// A 10s poll runs alongside Clear Stats' own reload, so an in-flight poll
// started before the clear can resolve after it and paint the pre-clear
// numbers straight back over the cleared ones. Same monotonic token
// SecurityPage/LibraryPage use: capture it at the start, apply the result
// only if it is still the newest request.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  try {
    const result = await getAiUsage()
    if (seq !== requestSeq) return
    usage.value = result
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.error-only handling */
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// Separate sequence token from load()'s: the two requests are independent
// and a slow periods fetch must not be discarded just because a usage poll
// landed in the meantime (and vice versa).
let periodsSeq = 0

async function loadPeriods() {
  const seq = ++periodsSeq
  periodsLoading.value = true
  try {
    const result = await getAiUsagePeriods()
    if (seq !== periodsSeq) return
    periods.value = result
    periodsFailed.value = false
  } catch {
    if (seq !== periodsSeq) return
    periodsFailed.value = true
  } finally {
    if (seq === periodsSeq) periodsLoading.value = false
  }
}

function onGranularityChange() {
  // Fetch the first time a period view is opened; afterwards the cached copy
  // renders instantly and the poll keeps it current.
  if (granularity.value !== 'day' && periods.value === null) void loadPeriods()
}

async function refresh() {
  await load()
  // Only while a period view is on screen — see the granularity comment above.
  if (granularity.value !== 'day') await loadPeriods()
}

let pollTimer: ReturnType<typeof setInterval>
onMounted(() => {
  void load()
  pollTimer = setInterval(refresh, 10_000)
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
    // Drop the cached rollups as well, or a cleared week/month keeps showing
    // its old totals until the next poll happens to refresh them.
    periods.value = null
    await load()
    if (granularity.value !== 'day') await loadPeriods()
  } catch {
    toast.show('Failed to clear usage stats', true)
  }
}
</script>

<template>
  <div class="auto-content">
    <h2 style="display: flex; align-items: center; justify-content: space-between; gap: 1rem">
      <span>AI Token Usage</span>
      <Button size="small" severity="danger" @click="clearUsage">🗑 Clear Stats</Button>
    </h2>

    <div v-if="loading" style="padding: 2rem"><LoadingIndicator /></div>
    <template v-else-if="usage">
      <EmptyState v-if="showDisabledMsg" title="📊 No AI Usage Data">
        Enable AI analysis in the add-on settings. Usage statistics will appear after the first analysis run.
      </EmptyState>
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

        <Card style="margin-bottom: 1.2rem">
          <template #title><h3 style="margin: 0; font: inherit; color: inherit">Current Provider</h3></template>
          <template #content>
            <div class="status-row">
              <span class="lbl">Provider</span><span class="val">{{ providerLabel(usage.provider) }}</span>
            </div>
            <div class="status-row">
              <span class="lbl">Model</span><span class="val">{{ usage.model || '—' }}</span>
            </div>
            <ProviderNote :provider="usage.provider" :show-escalation-note="showEscalationNote" />
          </template>
        </Card>

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
              <tr v-for="(m, i) in byModel" :key="i">
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
          <p v-if="!byModel.length" style="color: var(--muted); padding: 1rem; text-align: center">
            No analysis data yet. Run the AI analysis to see usage statistics.
          </p>
        </div>

        <div class="usage-history-head">
          <h3>
            Usage History <span class="usage-history-range">({{ historyMeta.heading }})</span>
          </h3>
          <SelectButton
            v-model="granularity"
            :options="GRANULARITY_OPTIONS"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            size="small"
            aria-label="Usage history granularity"
            @update:model-value="onGranularityChange"
          />
        </div>
        <div class="table-scroll">
          <table class="usage-table usage-history-table">
            <thead>
              <tr>
                <th>{{ historyMeta.column }}</th>
                <th style="text-align: right">Analyses</th>
                <th style="text-align: right">Total Tokens</th>
                <th style="text-align: right">Est. Cost</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in historyRows" :key="row.label">
                <td>{{ row.label }}</td>
                <td style="text-align: right">{{ fmtNum(row.analyses) }}</td>
                <td style="text-align: right">{{ fmtNum(row.tokens) }}</td>
                <td style="text-align: right">{{ fmtCost(row.cost) }}</td>
              </tr>
            </tbody>
            <tfoot v-if="historyRows.length">
              <tr class="usage-total-row">
                <td>Total</td>
                <td style="text-align: right">{{ fmtNum(historyTotals.analyses) }}</td>
                <td style="text-align: right">{{ fmtNum(historyTotals.tokens) }}</td>
                <td style="text-align: right">{{ fmtCost(historyTotals.cost) }}</td>
              </tr>
            </tfoot>
          </table>
          <div v-if="periodsLoading && !historyRows.length" style="padding: 1rem"><LoadingIndicator /></div>
          <p
            v-else-if="periodsFailed && granularity !== 'day'"
            style="color: var(--muted); padding: 1rem; text-align: center"
          >
            Could not load {{ granularity === 'week' ? 'weekly' : 'monthly' }} usage.
          </p>
          <p v-else-if="!historyRows.length" style="color: var(--muted); padding: 1rem; text-align: center">
            {{ historyMeta.empty }}
          </p>
        </div>
      </div>
    </template>
  </div>
</template>
