<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { getSecurityEvents } from '../../api/security'
import type { SecurityEventRow } from '../../api/types'
import { evidenceLabel, formatEventType, formatOffset, severityTag } from './severity'

const props = defineProps<{ clipId: string }>()
// Each listed event knows the second it was measured at, so the row it sits
// in can open the clip there. Scrubbing for "possible contact at 0:06" by
// hand is the difference between a log and something you can review.
const emit = defineEmits<{ seek: [seconds: number] }>()

const events = ref<SecurityEventRow[]>([])
const loading = ref(false)
const failed = ref(false)

// The timeline re-renders whenever it reloads, so an expanded row can be
// handed a different clip while its own request is still in flight. Same
// monotonic token SecurityPage uses: apply the result only if it is still
// the newest request, and default a malformed payload rather than trusting
// it — an `events` the backend omitted would otherwise throw on `[0]` below
// instead of rendering the "no events" state.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  loading.value = true
  failed.value = false
  try {
    const loaded = (await getSecurityEvents(props.clipId)).events ?? []
    if (seq !== requestSeq) return
    events.value = loaded
  } catch {
    if (seq === requestSeq) failed.value = true
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

watch(() => props.clipId, load, { immediate: true })

/** Every event carries the clip-level score, so the first row is as good a
 *  source as any — and there is no separate request to make for it. */
const summary = computed(() => events.value[0] ?? null)
</script>

<template>
  <div class="security-detail" data-testid="security-detail">
    <p v-if="loading" class="security-detail-note">Loading evidence…</p>
    <Message v-else-if="failed" severity="error" :closable="false">
      Could not load this clip's security evidence.
    </Message>
    <template v-else>
      <div v-if="summary" class="security-detail-scores">
        <span
          >Risk <strong>{{ Math.round(summary.risk_score) }}</strong
          >/100</span
        >
        <span>
          Evidence
          <strong>{{ Math.round(summary.evidence_quality * 100) }}%</strong>
          ({{ evidenceLabel(summary.evidence_quality) }})
        </span>
      </div>
      <p class="security-detail-note">
        Computed from object detection and tracking. The AI model judges the frames themselves and may disagree.
      </p>
      <ul class="security-detail-list">
        <li v-for="event in events" :key="event.id">
          <button
            type="button"
            class="security-detail-time"
            :aria-label="`Play from ${formatOffset(event.start_offset)}`"
            @click="emit('seek', event.start_offset)"
          >
            {{ formatOffset(event.start_offset) }}
          </button>
          <Tag :value="formatEventType(event.event_type)" :severity="severityTag(event.severity)" />
          <span class="security-detail-text">{{ event.detail }}</span>
          <span class="security-detail-confidence">{{ Math.round(event.confidence * 100) }}%</span>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
.security-detail {
  padding: 10px 0 4px;
}
.security-detail-scores {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 0.85rem;
  margin-bottom: 4px;
}
.security-detail-note {
  margin: 0 0 8px;
  font-size: 0.78rem;
  color: var(--text-muted);
}
.security-detail-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.security-detail-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 0.85rem;
}
.security-detail-time {
  background: none;
  border: 0;
  padding: 0;
  cursor: pointer;
  text-decoration: underline;
  text-underline-offset: 2px;
  font-variant-numeric: tabular-nums;
  color: var(--text-muted);
  min-width: 3em;
}
.security-detail-text {
  flex: 1 1 min(320px, 100%);
}
.security-detail-confidence {
  font-size: 0.78rem;
  color: var(--text-muted);
}
</style>
