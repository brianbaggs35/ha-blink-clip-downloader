<script setup lang="ts">
import { computed } from 'vue'
import type { SecurityStats, SecuritySeverity } from '../../api/types'
import { SECURITY_SEVERITIES } from '../../api/types'
import { SEVERITY_LABEL, severityColor } from './severity'

const props = defineProps<{ stats: SecurityStats | null }>()

/** Always all four bands, most severe first, including the zeroes. A row
 *  that silently omits "critical" when the count is zero reads as missing
 *  data rather than as good news — and "0 critical" is exactly the thing
 *  someone opening this tab wants to see confirmed. */
const bands = computed(() =>
  SECURITY_SEVERITIES.map((severity: SecuritySeverity) => ({
    severity,
    label: SEVERITY_LABEL[severity],
    count: props.stats?.by_severity?.[severity] ?? 0,
  })).reverse(),
)

/** Proportions at a glance, above the exact counts. Empty when nothing has
 *  been recorded, which hides the bar entirely — a track with no segments
 *  in it reads as a broken chart rather than as "no events".
 *
 *  Hand-rolled rather than PrimeVue's MeterGroup: its own legend cannot be
 *  turned off (there is no `labelsVisible` prop — only `labelPosition` and
 *  `labelOrientation`), so the bar always came with a second, redundant
 *  copy of the counts sitting beside the tiles below. */
const segments = computed(() =>
  bands.value
    .filter((band) => band.count > 0)
    .map((band) => ({
      label: band.label,
      count: band.count,
      color: severityColor(band.severity),
    })),
)

/** The bar's own total, rather than the API's `total` field: the segments
 *  are the whole of what the bar shows, so summing them is both correct and
 *  free of a "what if stats is null here" branch that cannot happen. */
const segmentTotal = computed(() => segments.value.reduce((sum, segment) => sum + segment.count, 0))

function share(count: number): string {
  return `${Math.round((count / segmentTotal.value) * 100)}%`
}
</script>

<template>
  <section class="security-stats" data-testid="security-stats">
    <header class="security-stats-head">
      <h3 class="security-stats-title">Recent activity</h3>
      <p class="security-stats-note">
        {{ stats ? `${stats.total} clip(s) with security events in the last ${stats.days} days` : '—' }}
      </p>
    </header>

    <div v-if="segments.length" class="security-meter" data-testid="security-meter">
      <span
        v-for="segment in segments"
        :key="segment.label"
        class="security-meter-seg"
        :style="{ flexGrow: segment.count, background: segment.color }"
        :title="`${segment.label}: ${segment.count} (${share(segment.count)})`"
      />
    </div>

    <div class="security-stats-bands">
      <div
        v-for="band in bands"
        :key="band.severity"
        class="security-stat"
        :class="{ 'is-zero': !band.count }"
        :style="{ '--sev': severityColor(band.severity), '--sev-soft': severityColor(band.severity, 0.14) }"
      >
        <span class="security-stat-count">{{ band.count }}</span>
        <span class="security-stat-label">{{ band.label }}</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
.security-stats {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 0.9rem 1rem 1rem;
  margin-bottom: 1rem;
}
.security-stats-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.35rem 1rem;
  margin-bottom: 0.7rem;
}
.security-stats-title {
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
}
.security-stats-note {
  margin: 0;
  font-size: 0.8rem;
  color: var(--muted);
}
/* The proportion bar. A rounded track with the segments butted together
   inside it, so a one-band window still reads as a full bar rather than as
   a stray coloured line. */
.security-meter {
  display: flex;
  height: 8px;
  border-radius: 999px;
  overflow: hidden;
  background: var(--card2);
  margin-bottom: 0.85rem;
}
.security-meter-seg {
  flex-basis: 0;
  min-width: 3px;
}
.security-stats-bands {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(130px, 100%), 1fr));
  gap: 0.6rem;
}
.security-stat {
  display: flex;
  flex-direction: column;
  gap: 0.1rem;
  padding: 0.5rem 0.7rem;
  border-radius: var(--radius-sm);
  background: var(--sev-soft);
  border-left: 3px solid var(--sev);
}
/* A band with nothing in it is good news, not a headline — kept legible
   but visibly quieter than one that has something to say. */
.security-stat.is-zero {
  background: var(--card2);
  border-left-color: var(--border-strong);
}
.security-stat-count {
  font-size: 1.45rem;
  font-weight: 700;
  line-height: 1.1;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.security-stat.is-zero .security-stat-count {
  color: var(--muted);
}
.security-stat-label {
  font-size: 0.75rem;
  color: var(--muted);
  letter-spacing: 0.01em;
}
</style>
