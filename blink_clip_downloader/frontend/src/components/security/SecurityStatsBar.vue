<script setup lang="ts">
import { computed } from 'vue'
import Tag from 'primevue/tag'
import type { SecurityStats, SecuritySeverity } from '../../api/types'
import { SECURITY_SEVERITIES } from '../../api/types'
import { SEVERITY_LABEL, SEVERITY_TAG } from './severity'

const props = defineProps<{ stats: SecurityStats | null }>()

/** Always all four bands, in severity order, including the zeroes. A row
 *  that silently omits "critical" when the count is zero reads as missing
 *  data rather than as good news — and "0 critical" is exactly the thing
 *  someone opening this tab wants to see confirmed. */
const bands = computed(() =>
  SECURITY_SEVERITIES.map((severity: SecuritySeverity) => ({
    severity,
    label: SEVERITY_LABEL[severity],
    tag: SEVERITY_TAG[severity],
    count: props.stats?.by_severity?.[severity] ?? 0,
  })).reverse(),
)
</script>

<template>
  <div class="security-stats" data-testid="security-stats">
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
