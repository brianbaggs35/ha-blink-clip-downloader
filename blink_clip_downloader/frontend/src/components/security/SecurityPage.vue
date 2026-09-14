<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Timeline from 'primevue/timeline'
import { getSecurityStats, getSecurityTimeline } from '../../api/security'
import { clipThumbUrl, getCameras } from '../../api/clips'
import type { SecurityStats, SecurityTimelineRow } from '../../api/types'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import SecurityEventDetail from './SecurityEventDetail.vue'
import SecurityStatsBar from './SecurityStatsBar.vue'
import { formatEventType, formatOffset, severityTag } from './severity'

const PAGE_SIZE = 25

const clipViewer = useClipViewerStore()
const refresh = useRefreshStore()
const toast = useToastStore()

const loading = ref(true)
const loadingMore = ref(false)
const failed = ref(false)
const rows = ref<SecurityTimelineRow[]>([])
const total = ref(0)
const stats = ref<SecurityStats | null>(null)
const cameras = ref<string[]>([])
const expanded = ref<string | null>(null)

const camera = ref<string | null>(null)
const severity = ref<string | null>(null)
const period = ref<string | null>(null)

const SEVERITY_OPTIONS = [
  { label: 'Any severity', value: null },
  { label: 'Noteworthy and up', value: 'noteworthy' },
  { label: 'Suspicious and up', value: 'suspicious' },
  { label: 'Critical only', value: 'critical' },
]

const PERIOD_OPTIONS = [
  { label: 'All', value: null },
  { label: 'Today', value: 'today' },
  { label: 'Week', value: 'week' },
  { label: 'Month', value: 'month' },
]

const cameraOptions = computed(() => [
  { label: 'All cameras', value: null },
  ...cameras.value.map((name) => ({ label: name, value: name })),
])

const hasMore = computed(() => rows.value.length < total.value)

// Three independent triggers fire load() with no inherent ordering — the
// initial mount, the filter watcher, and refresh.tick — so a slower earlier
// request can resolve after a newer one and repopulate the tab with rows
// for a filter the user has already moved off. Same monotonic token
// LibraryPage uses for the same reason: capture it at the start, apply the
// result only if still the newest request.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  loading.value = true
  failed.value = false
  try {
    const [timeline, statsResult] = await Promise.all([
      getSecurityTimeline({
        limit: PAGE_SIZE,
        camera: camera.value ?? undefined,
        severity: severity.value ?? undefined,
        period: period.value ?? undefined,
      }),
      getSecurityStats(),
    ])
    // Defaulted rather than trusted: a response missing `events` (an older
    // backend, a proxy returning something unexpected) would otherwise blank
    // the whole tab with a render error instead of the empty state.
    if (seq !== requestSeq) return
    rows.value = timeline.events ?? []
    total.value = timeline.total ?? 0
    stats.value = statsResult
  } catch {
    if (seq === requestSeq) failed.value = true
  } finally {
    if (seq === requestSeq) {
      loading.value = false
      // A reload replaces the whole list, so any page-append still in
      // flight is moot — its result is dropped by the same token below, and
      // its spinner (which also disables the button) must not outlive it.
      loadingMore.value = false
    }
  }
}

async function loadMore() {
  const seq = ++requestSeq
  loadingMore.value = true
  try {
    const page = await getSecurityTimeline({
      limit: PAGE_SIZE,
      offset: rows.value.length,
      camera: camera.value ?? undefined,
      severity: severity.value ?? undefined,
      period: period.value ?? undefined,
    })
    // Appending a page fetched under the previous filter would splice rows
    // the user has already filtered away back into the list.
    if (seq !== requestSeq) return
    // Offset paging over a list that grows at the top: a clip analyzed
    // between the two requests shifts every row down one, so the next page
    // starts by repeating the row that was last on this one. One row per
    // clip is the property the whole timeline query is built around, so
    // drop repeats rather than render the same clip twice.
    const seen = new Set(rows.value.map((row) => row.clip_id))
    const fresh = (page.events ?? []).filter((row) => !seen.has(row.clip_id))
    rows.value = [...rows.value, ...fresh]
    total.value = page.total ?? rows.value.length
  } catch {
    if (seq === requestSeq) toast.show('Failed to load more events', true)
  } finally {
    if (seq === requestSeq) loadingMore.value = false
  }
}

async function loadCameras() {
  try {
    cameras.value = (await getCameras()).map((c) => c.camera)
  } catch {
    cameras.value = []
  }
}

onMounted(async () => {
  await Promise.all([loadCameras(), load()])
})

watch([camera, severity, period], () => {
  expanded.value = null
  void load()
})
watch(
  () => refresh.tick,
  () => void load(),
)

function toggle(row: SecurityTimelineRow) {
  expanded.value = expanded.value === row.clip_id ? null : row.clip_id
}

/** Opens the Library tab's clip modal in place, without switching tabs —
 *  the same cross-tab bridge the AI tab's suspicious feed uses. Without it,
 *  a timeline entry names a clip and then leaves you to go and find it. */
function openClip(row: SecurityTimelineRow, startAt: number | null = null) {
  clipViewer.requestOpen(row.clip_id, startAt)
}

/** What the AI model concluded about the same clip, phrased for a badge.
 *  `null` means the clip has no analysis row at all, which is possible for
 *  a clip whose events were written by an older build. */
function verdict(row: SecurityTimelineRow): { label: string; severity: string } | null {
  if (row.ai_suspicious == null) return null
  if (row.risk_override_applied) return { label: 'Flagged on evidence', severity: 'warn' }
  if (row.ai_suspicious) return { label: 'AI: suspicious', severity: 'danger' }
  return { label: 'AI: nothing unusual', severity: 'secondary' }
}

/** True when code and model reached opposite conclusions. Worth pointing at
 *  directly: a clip the geometry rates highly and the model waved through —
 *  or the reverse — is the one a person most needs to look at themselves.
 *  A risk override is excluded because that is not a disagreement left
 *  standing: the clip was already flagged on the evidence. */
function disagrees(row: SecurityTimelineRow): boolean {
  if (row.ai_suspicious == null || row.risk_override_applied) return false
  const codeConcerned = row.severity === 'critical' || row.severity === 'suspicious'
  return codeConcerned !== row.ai_suspicious
}

const thumbFailed = ref<Record<string, boolean>>({})
function onThumbError(clipId: string) {
  thumbFailed.value = { ...thumbFailed.value, [clipId]: true }
}

function formatWhen(timestamp: string): string {
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return timestamp
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}
</script>

<template>
  <div class="security-page">
    <h2>Security Timeline</h2>
    <p class="security-intro">
      What object detection and tracking observed across your cameras, most recent first. This is code-computed evidence
      — the AI model's own verdict on each clip is shown in the Library.
    </p>

    <SecurityStatsBar :stats="stats" />

    <div class="security-filters">
      <Select
        v-model="camera"
        :options="cameraOptions"
        option-label="label"
        option-value="value"
        aria-label="Filter by camera"
        class="security-filter"
      />
      <Select
        v-model="severity"
        :options="SEVERITY_OPTIONS"
        option-label="label"
        option-value="value"
        aria-label="Filter by severity"
        class="security-filter"
      />
      <SelectButton
        v-model="period"
        :options="PERIOD_OPTIONS"
        option-label="label"
        option-value="value"
        :allow-empty="false"
        aria-label="Filter by period"
      />
    </div>

    <LoadingIndicator v-if="loading" />
    <Message v-else-if="failed" severity="error" :closable="false"> Failed to load the security timeline. </Message>
    <Message v-else-if="!rows.length" severity="secondary" :closable="false">
      No security events yet. They appear once clips are analyzed with Enhanced Detection and Structured Security
      Analysis enabled.
    </Message>

    <!-- The test id lives on this wrapper rather than on <Timeline>: a
         PrimeVue component does not reliably forward arbitrary attributes
         to its rendered root, so scoping to it directly matched nothing in
         a real browser even while the rows were plainly on screen. -->
    <div v-else data-testid="security-timeline">
      <Timeline :value="rows" class="security-timeline">
        <template #opposite="{ item }">
          <span class="security-when">{{ formatWhen(item.clip_timestamp) }}</span>
        </template>
        <template #content="{ item }">
          <div class="security-row">
            <div class="security-row-body">
              <button
                v-if="!thumbFailed[item.clip_id]"
                type="button"
                class="security-thumb"
                :aria-label="`Open the clip from ${item.camera}`"
                @click="openClip(item)"
              >
                <img :src="clipThumbUrl(item.clip_id)" loading="lazy" alt="" @error="onThumbError(item.clip_id)" />
              </button>
              <div class="security-row-main">
                <div class="security-row-head">
                  <span class="security-when-inline">{{ formatWhen(item.clip_timestamp) }}</span>
                  <Tag :value="formatEventType(item.event_type)" :severity="severityTag(item.severity)" />
                  <span class="security-camera">{{ item.camera }}</span>
                  <span class="security-risk">Risk {{ Math.round(item.risk_score) }}</span>
                  <Tag
                    v-if="verdict(item)"
                    :value="verdict(item)!.label"
                    :severity="verdict(item)!.severity"
                    class="security-verdict"
                  />
                  <span
                    v-if="disagrees(item)"
                    class="security-disagree"
                    title="Code and model reached opposite conclusions"
                  >
                    ⚠ disagreement
                  </span>
                </div>
                <p class="security-detail-line">{{ item.detail }}</p>
                <p v-if="item.ai_summary" class="security-ai-line">“{{ item.ai_summary }}”</p>
                <div class="security-row-actions">
                  <Button
                    :label="item.start_offset > 0 ? `View clip at ${formatOffset(item.start_offset)}` : 'View clip'"
                    size="small"
                    severity="secondary"
                    outlined
                    @click="openClip(item, item.start_offset)"
                  />
                  <Button
                    :label="expanded === item.clip_id ? 'Hide evidence' : 'Show evidence'"
                    size="small"
                    text
                    @click="toggle(item)"
                  />
                </div>
              </div>
            </div>
            <SecurityEventDetail
              v-if="expanded === item.clip_id"
              :clip-id="item.clip_id"
              @seek="openClip(item, $event)"
            />
          </div>
        </template>
      </Timeline>
    </div>

    <div v-if="hasMore && !loading" class="security-more">
      <Button label="Load more" severity="secondary" outlined :loading="loadingMore" @click="loadMore" />
    </div>
  </div>
</template>

<style scoped>
.security-page {
  padding: 4px 0 24px;
}
.security-intro {
  margin: 0 0 14px;
  font-size: 0.85rem;
  color: var(--text-muted);
  max-width: 70ch;
}
.security-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}
.security-filter {
  min-width: min(200px, 100%);
}
.security-when {
  font-size: 0.8rem;
  color: var(--text-muted);
  white-space: nowrap;
}
/* PrimeVue's Timeline gives the "opposite" column a fixed share of the
   row, which on a phone leaves the event itself a column barely wide
   enough for one word. Below that width the timestamp moves into the row
   itself and the opposite column is dropped entirely. */
.security-when-inline {
  display: none;
  font-size: 0.8rem;
  color: var(--text-muted);
}
@media (max-width: 700px) {
  .security-when-inline {
    display: inline;
    flex-basis: 100%;
  }
  .security-page :deep(.p-timeline-event-opposite) {
    display: none;
  }
}
.security-row-body {
  display: flex;
  gap: 12px;
  align-items: flex-start;
}
.security-row-main {
  flex: 1;
  min-width: 0;
}
.security-thumb {
  flex: 0 0 auto;
  padding: 0;
  border: 0;
  background: none;
  cursor: pointer;
  line-height: 0;
  border-radius: 6px;
  overflow: hidden;
}
.security-thumb img {
  width: 112px;
  height: 63px;
  object-fit: cover;
  display: block;
}
.security-ai-line {
  margin: 2px 0 0;
  font-size: 0.85rem;
  opacity: 0.75;
  font-style: italic;
}
.security-disagree {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--p-amber-600, #d97706);
  white-space: nowrap;
}
@media (max-width: 640px) {
  .security-thumb img {
    width: 74px;
    height: 42px;
  }
}
.security-row-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.security-camera {
  font-weight: 600;
}
.security-risk {
  font-size: 0.8rem;
  color: var(--text-muted);
}
.security-detail-line {
  margin: 6px 0;
  font-size: 0.88rem;
}
.security-row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.security-more {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}
</style>
