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
        <div class="security-score">
          <span class="security-score-label">Risk</span>
          <span class="security-score-value">{{ Math.round(summary.risk_score) }}<small>/100</small></span>
          <span class="security-score-track">
            <span class="security-score-fill is-risk" :style="{ width: `${Math.round(summary.risk_score)}%` }" />
          </span>
        </div>
        <div class="security-score">
          <span class="security-score-label">Evidence</span>
          <span class="security-score-value">
            {{ Math.round(summary.evidence_quality * 100)
            }}<small>% {{ evidenceLabel(summary.evidence_quality) }}</small>
          </span>
          <span class="security-score-track">
            <span class="security-score-fill" :style="{ width: `${Math.round(summary.evidence_quality * 100)}%` }" />
          </span>
        </div>
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
          <span class="security-detail-confidence" title="How sure the detector is, given the evidence it had">
            {{ Math.round(event.confidence * 100) }}%
          </span>
        </li>
      </ul>
    </template>
  </div>
</template>

<style scoped>
/* Sits inside the timeline row's own card, so it opens as an inset panel
   ruled off from the row above rather than as loose text running into it. */
.security-detail {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid var(--border);
}
.security-detail-scores {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(200px, 100%), 1fr));
  gap: 0.5rem 1.1rem;
  margin-bottom: 0.6rem;
}
.security-score {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 0.1rem 0.5rem;
}
.security-score-label {
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
.security-score-value {
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.security-score-value small {
  font-size: 0.72rem;
  font-weight: 500;
  color: var(--muted);
}
/* Both numbers are 0-100 scales, so a bar apiece says "how much of the
   way" at a glance in a way two bare percentages do not. */
.security-score-track {
  grid-column: 1 / -1;
  height: 4px;
  border-radius: 999px;
  background: var(--card-hover);
  overflow: hidden;
  margin-top: 0.15rem;
}
.security-score-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
}
/* Risk is the row's own severity band; evidence quality is not a severity
   at all, so it stays on the neutral accent above. */
.security-score-fill.is-risk {
  background: var(--sev, var(--accent));
}
.security-detail-note {
  margin: 0 0 0.6rem;
  font-size: 0.76rem;
  color: var(--muted);
}
.security-detail-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.security-detail-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.82rem;
  color: var(--text-dim);
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.4rem 0.55rem;
}
/* A time that opens the clip there is an action, so it looks like one —
   the bare underlined number it used to be read as body text. */
.security-detail-time {
  background: var(--card2);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
  cursor: pointer;
  font-variant-numeric: tabular-nums;
  font-size: 0.75rem;
  color: var(--text-dim);
  transition:
    color 0.15s var(--ease),
    border-color 0.15s var(--ease);
}
.security-detail-time:hover {
  color: var(--text);
  border-color: var(--accent);
}
.security-detail-text {
  flex: 1 1 min(320px, 100%);
}
.security-detail-confidence {
  font-size: 0.75rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
</style>
