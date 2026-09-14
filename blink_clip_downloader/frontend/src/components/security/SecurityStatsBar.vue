<script setup lang="ts">
import { computed } from 'vue'
import MeterGroup from 'primevue/metergroup'
import Tag from 'primevue/tag'
import type { SecurityStats, SecuritySeverity } from '../../api/types'
import { SECURITY_SEVERITIES } from '../../api/types'
import { SEVERITY_LABEL, SEVERITY_TAG } from './severity'

const props = defineProps<{ stats: SecurityStats | null }>()

/** Always all four bands, in severity order, including the zeroes. A row
 *  that silently omits "critical" when the count is zero reads as missing
 *  data rather than as good news — and "0 critical" is exactly the thing
 *  someone opening this tab wants to see confirmed. */
/** Colours the meter segments. Deliberately the same four hues the theme
 *  gives PrimeVue's secondary/info/warn/danger tags, so the bar and the
 *  counts beside it are visibly the same four things. */
const METER_COLOR: Record<SecuritySeverity, string> = {
  routine: '#64748b',
  noteworthy: '#3b82f6',
  suspicious: '#f59e0b',
  critical: '#ef4444',
}

const bands = computed(() =>
  SECURITY_SEVERITIES.map((severity: SecuritySeverity) => ({
    severity,
    label: SEVERITY_LABEL[severity],
    tag: SEVERITY_TAG[severity],
    count: props.stats?.by_severity?.[severity] ?? 0,
  })).reverse(),
)
/** Proportions at a glance, above the exact counts. Hidden entirely when
 *  nothing has been recorded — an empty meter is a horizontal line that
 *  reads as a broken chart rather than as "no events". */
const meter = computed(() =>
  bands.value
    .filter((band) => band.count > 0)
    .map((band) => ({
      label: band.label,
      value: band.count,
      color: METER_COLOR[band.severity],
    })),
)

/** The bar's own total, rather than the API's `total` field: the segments
 *  are the whole of what the bar shows, so summing them is both correct and
 *  free of a "what if stats is null here" branch that cannot happen — the
 *  meter only renders when there are bands, and bands only exist when stats
 *  loaded. */
const meterTotal = computed(() => meter.value.reduce((sum, segment) => sum + segment.value, 0))
</script>

<template>
  <div class="security-stats" data-testid="security-stats">
    <MeterGroup
      v-if="meter.length"
      :value="meter"
      :max="meterTotal"
      :labels-visible="false"
      class="security-meter"
      data-testid="security-meter"
    />
    <div class="security-stats-bands">
      <div v-for="band in bands" :key="band.severity" class="security-stat">
        <Tag :value="String(band.count)" :severity="band.tag" />
        <span class="security-stat-label">{{ band.label }}</span>
      </div>
    </div>
    <p class="security-stats-note">
      {{ stats ? `${stats.total} clip(s) with security events in the last ${stats.days} days` : '—' }}
    </p>
  </div>
</template>

<style scoped>
.security-meter {
  flex: 1 1 100%;
  max-width: 420px;
}
.security-stats {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 18px;
  margin-bottom: 14px;
}
.security-stats-bands {
  display: flex;
  flex-wrap: wrap;
  gap: 14px;
}
.security-stat {
  display: flex;
  align-items: center;
  gap: 6px;
}
.security-stat-label {
  font-size: 0.85rem;
  color: var(--text-muted);
}
.security-stats-note {
  margin: 0;
  font-size: 0.8rem;
  color: var(--text-muted);
}
</style>
