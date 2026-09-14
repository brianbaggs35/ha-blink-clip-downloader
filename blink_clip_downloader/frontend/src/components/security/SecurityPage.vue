<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import Timeline from 'primevue/timeline'
import { getSecurityStats, getSecurityTimeline } from '../../api/security'
import { getCameras } from '../../api/clips'
import type { SecurityStats, SecurityTimelineRow } from '../../api/types'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import SecurityEventDetail from './SecurityEventDetail.vue'
import SecurityStatsBar from './SecurityStatsBar.vue'
import { formatEventType, severityTag } from './severity'

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
    rows.value = [...rows.value, ...(page.events ?? [])]
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
function openClip(row: SecurityTimelineRow) {
  clipViewer.requestOpen(row.clip_id)
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
            <div class="security-row-head">
              <span class="security-when-inline">{{ formatWhen(item.clip_timestamp) }}</span>
              <Tag :value="formatEventType(item.event_type)" :severity="severityTag(item.severity)" />
              <span class="security-camera">{{ item.camera }}</span>
              <span class="security-risk">Risk {{ Math.round(item.risk_score) }}</span>
            </div>
            <p class="security-detail-line">{{ item.detail }}</p>
            <div class="security-row-actions">
              <Button label="View clip" size="small" severity="secondary" outlined @click="openClip(item)" />
              <Button
                :label="expanded === item.clip_id ? 'Hide evidence' : 'Show evidence'"
                size="small"
                text
                @click="toggle(item)"
              />
            </div>
            <SecurityEventDetail v-if="expanded === item.clip_id" :clip-id="item.clip_id" />
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
