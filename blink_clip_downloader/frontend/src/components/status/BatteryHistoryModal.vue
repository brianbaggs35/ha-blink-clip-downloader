<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Dialog from 'primevue/dialog'
import { getBatteryHistory } from '../../api/battery'
import type { BatteryHistoryEntry } from '../../api/types'
import { fmtTs } from '../../api/constants'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

const props = defineProps<{ camera: string }>()
const emit = defineEmits<{ close: [] }>()

const loading = ref(true)
const history = ref<BatteryHistoryEntry[]>([])

interface HistoryRow {
  key: string
  low: boolean
  label: string
  timestamp: string
  /** Only set for a "low" row — how long it stayed low before the next
   * (chronologically later) recovery, or "Ongoing" if still low. Directly
   * answers "how long are your batteries lasting". */
  duration: string | null
}

/** history is newest-first (see ClipDatabase.get_battery_history), so the
 * "next" recovery for a low row at index i is the chronologically-later
 * entry at index i-1. */
function formatDuration(fromIso: string, toIso: string): string {
  const ms = new Date(toIso).getTime() - new Date(fromIso).getTime()
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  const hours = Math.floor(ms / 3_600_000)
  const days = Math.floor(hours / 24)
  if (days > 0) return `${days}d ${hours % 24}h`
  if (hours > 0) return `${hours}h`
  return '<1h'
}

const rows = computed<HistoryRow[]>(() =>
  history.value.map((entry, i) => {
    const low = entry.battery_state === 'low'
    let duration: string | null = null
    if (low) {
      const recoveredAt = i > 0 ? history.value[i - 1].recorded_at : null
      duration = recoveredAt ? formatDuration(entry.recorded_at, recoveredAt) : 'Ongoing'
    }
    return {
      key: `${entry.camera}-${entry.recorded_at}`,
      low,
      label: low ? 'Went low' : 'Back to normal',
      timestamp: entry.recorded_at,
      duration,
    }
  }),
)

async function load() {
  loading.value = true
  try {
    history.value = await getBatteryHistory(props.camera)
  } catch {
    history.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <Dialog
    :visible="true"
    modal
    dismissable-mask
    :header="`${camera} — Battery History`"
    :style="{ width: '32rem' }"
    :draggable="false"
    @update:visible="emit('close')"
  >
    <div v-if="loading" style="padding: 1rem"><LoadingIndicator /></div>
    <p v-else-if="!rows.length" style="color: var(--muted); font-size: 0.85rem">
      No battery state changes recorded yet for this camera.
    </p>
    <table v-else style="width: 100%; border-collapse: collapse; font-size: 0.83rem">
      <thead>
        <tr>
          <th scope="col" class="sr-only">Event</th>
          <th scope="col" class="sr-only">When</th>
          <th scope="col" class="sr-only">Duration low</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="(row, i) in rows"
          :key="row.key"
          :style="i < rows.length - 1 ? 'border-bottom: 1px solid var(--border)' : ''"
        >
          <td :style="{ padding: '0.4rem 0.5rem', color: row.low ? 'var(--danger)' : 'var(--success)' }">
            {{ row.label }}
          </td>
          <td style="padding: 0.4rem 0.5rem; color: var(--muted)">{{ fmtTs(row.timestamp) }}</td>
          <td style="padding: 0.4rem 0.5rem; text-align: right">{{ row.duration ?? '—' }}</td>
        </tr>
      </tbody>
    </table>
  </Dialog>
</template>
