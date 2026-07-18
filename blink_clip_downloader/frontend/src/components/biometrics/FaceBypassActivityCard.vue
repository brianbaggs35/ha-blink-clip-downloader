<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import { getFaceBypassStats, getFaceRecognitionFeedback } from '../../api/ai'
import type { FaceBypassStats, FaceRecognitionFeedback } from '../../api/types'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// Auditability for the suspicious-flag bypass (see analyzer.py's
// _face_bypass_applies docstring): the bypass being all-or-nothing and
// name-free-in-prompts is a safety guarantee, but it's still worth being
// able to see, locally, whether it's actually firing for the right people
// over time — this card is that visibility, not a control surface.
const loading = ref(true)
const loadError = ref(false)
const stats = ref<FaceBypassStats | null>(null)
// Reports filed from a clip's AI panel ("Wrong match" / "Report a missed
// face match" — see ClipAiPanel.vue's reportFaceIssue). This is a pure
// audit trail (see database.py's face_recognition_feedback schema comment)
// — surfaced here for a human to notice and act on, not auto-applied.
const feedback = ref<FaceRecognitionFeedback[]>([])

async function load() {
  loading.value = true
  loadError.value = false
  try {
    stats.value = await getFaceBypassStats()
    // Secondary data — if it fails to load, still show the bypass stats
    // above rather than failing the whole card.
    feedback.value = await getFaceRecognitionFeedback().catch(() => [])
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)

function fmtTs(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}
</script>

<template>
  <Card class="bypass-activity-card">
    <template #title>🪪 Face-bypass activity</template>
    <template #subtitle>
      Every clip where an approved household member's face cleared the suspicious flag — check this occasionally to
      confirm it's only ever firing for people you actually enrolled.
    </template>
    <template #content>
      <div v-if="loading" style="padding: 0.5rem"><LoadingIndicator /></div>
      <div v-else-if="loadError" class="muted-note">Failed to load bypass activity.</div>
      <template v-else-if="stats">
        <div v-if="stats.total_bypassed === 0" class="muted-note">
          No suspicious-flag bypass has fired yet. It will show up here the first time an approved, recognized person is
          the only one visible in a clip the AI would otherwise have flagged.
        </div>
        <template v-else>
          <div class="bypass-summary-row">
            <span class="bypass-total">{{ stats.total_bypassed }}</span>
            <span class="muted-note">total bypass(es) recorded</span>
          </div>
          <div v-if="stats.by_name.length" class="bypass-name-tags">
            <Tag
              v-for="row in stats.by_name"
              :key="row.name"
              severity="secondary"
              :value="`${row.name} × ${row.count}`"
            />
          </div>
          <ul class="bypass-recent-list">
            <li v-for="event in stats.recent" :key="`${event.clip_id}-${event.analyzed_at}`">
              <strong>{{ event.face_bypass_names }}</strong>
              on <em>{{ event.camera }}</em>
              <span class="muted-note">— {{ fmtTs(event.analyzed_at) }}</span>
            </li>
          </ul>
        </template>

        <div class="bypass-feedback-section">
          <div class="bypass-feedback-hdr">🚩 Reported accuracy issues</div>
          <div v-if="feedback.length === 0" class="muted-note">
            No accuracy reports yet — "Wrong match" and "Report a missed face match" on a clip's AI panel show up here.
          </div>
          <ul v-else class="bypass-recent-list">
            <li v-for="row in feedback" :key="`${row.clip_id}-${row.created_at}`">
              <Tag
                :severity="row.report_type === 'false_positive' ? 'danger' : 'warn'"
                :value="row.report_type === 'false_positive' ? 'Wrong match' : 'Missed match'"
              />
              <strong v-if="row.person_name">{{ row.person_name }}</strong>
              on <em>{{ row.camera }}</em>
              <span class="muted-note">— {{ fmtTs(row.created_at) }}</span>
              <span v-if="row.note" class="muted-note">— "{{ row.note }}"</span>
            </li>
          </ul>
        </div>
      </template>
    </template>
  </Card>
</template>

<style scoped>
.bypass-activity-card {
  margin-bottom: 1rem;
}
.bypass-summary-row {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  margin-bottom: 0.6rem;
}
.bypass-total {
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--accent, #7c5cff);
}
.bypass-name-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.7rem;
}
.bypass-recent-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  font-size: 0.85rem;
  max-height: 220px;
  overflow-y: auto;
}
.bypass-feedback-section {
  margin-top: 0.8rem;
  padding-top: 0.7rem;
  border-top: 1px solid var(--border);
}
.bypass-feedback-hdr {
  font-weight: 600;
  font-size: 0.85rem;
  margin-bottom: 0.5rem;
}
.muted-note {
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
