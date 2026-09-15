<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Tag from 'primevue/tag'
import { getSecurityStats, getSecurityTimeline } from '../../api/security'
import { clipThumbUrl, getCameras } from '../../api/clips'
import type { SecurityTimelineRow, SecurityStats } from '../../api/types'
import { useClipViewerStore } from '../../stores/clipViewer'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import AppIcon from '../icons/AppIcon.vue'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import SecurityEventDetail from './SecurityEventDetail.vue'
import SecurityStatsBar from './SecurityStatsBar.vue'
import { formatEventType, formatOffset, severityColor, severityTag } from './severity'

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

/** True when the view is narrowed by anything the user chose. Drives the
 *  empty state: "no security events yet" is a claim about the whole
 *  install, and telling someone who has just filtered to "Critical only"
 *  that nothing has ever been analyzed is simply wrong. */
const filtered = computed(() => camera.value !== null || severity.value !== null || period.value !== null)

function clearFilters() {
  camera.value = null
  severity.value = null
  period.value = null
}

// Three independent triggers fire load() with no inherent ordering — the
// initial mount, the filter watcher, and refresh.tick — so a slower earlier
// request can resolve after a newer one and repopulate the tab with rows
// for a filter the user has already moved off. Same monotonic token
// LibraryPage uses for the same reason: capture it at the start, apply the
// result only if still the newest request.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  // Only while there is nothing on screen yet. refresh.tick fires from other
  // tabs' actions — including the clip modal this very timeline opens, which
  // bumps it on delete, analyze and feedback — so flipping this
  // unconditionally replaced a timeline someone was reading (expanded
  // evidence rows, scroll position and all) with a spinner for something
  // they never asked to reload. Same treatment StatusPage received.
  if (!rows.value.length) loading.value = true
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

/** The row's own band, as the three custom properties everything coloured
 *  inside it reads from: the rail dot, the card's edge and the risk pill.
 *  Set from here rather than from a CSS class per band so `severity.ts`
 *  stays the one place the four hues are written down. */
function severityVars(row: SecurityTimelineRow): Record<string, string> {
  return {
    '--sev': severityColor(row.severity),
    '--sev-soft': severityColor(row.severity, 0.14),
    '--sev-line': severityColor(row.severity, 0.38),
  }
}

const thumbFailed = ref<Record<string, boolean>>({})
function onThumbError(clipId: string) {
  thumbFailed.value = { ...thumbFailed.value, [clipId]: true }
}

function parsed(timestamp: string): Date | null {
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime()) ? null : date
}

/** Time of day alone — the date is carried by the day heading the row sits
 *  under, and repeating it on every row was most of what made the old
 *  timestamp column so wide. Falls back to the raw string for a timestamp
 *  that cannot be parsed, rather than rendering "Invalid Date". */
function formatWhen(timestamp: string): string {
  const date = parsed(timestamp)
  if (!date) return timestamp
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

/** "Today" / "Yesterday" / "Monday, Sep 15" for a day heading. */
function formatDay(timestamp: string): string {
  const date = parsed(timestamp)
  if (!date) return timestamp
  const days = Math.round((startOfDay(new Date()) - startOfDay(date)) / 86_400_000)
  if (days === 0) return 'Today'
  if (days === 1) return 'Yesterday'
  return date.toLocaleDateString(undefined, { weekday: 'long', month: 'short', day: 'numeric' })
}

function dayKey(timestamp: string): string {
  const date = parsed(timestamp)
  return date ? String(startOfDay(date)) : timestamp
}

/** Rows split into consecutive runs of the same day, so the feed reads as a
 *  diary rather than as one undifferentiated list. Rows arrive newest-first
 *  and stay in that order, so comparing against the open group is enough —
 *  no sorting, and a day that somehow recurs later simply starts a new
 *  heading rather than being silently reordered under the earlier one. */
const days = computed(() => {
  const groups: { key: string; label: string; rows: SecurityTimelineRow[] }[] = []
  for (const row of rows.value) {
    const key = dayKey(row.clip_timestamp)
    const open = groups.at(-1)
    if (open && open.key === key) open.rows.push(row)
    else groups.push({ key, label: formatDay(row.clip_timestamp), rows: [row] })
  }
  return groups
})
</script>

<template>
  <div class="security-page">
    <h2>Security Events</h2>
    <p class="page-intro">
      What object detection and tracking observed across your cameras, most recent first. This is code-computed evidence
      — the AI model's own verdict on each clip is shown in the Library.
    </p>

    <SecurityStatsBar :stats="stats" />

    <div class="security-toolbar">
      <Select
        v-model="camera"
        :options="cameraOptions"
        option-label="label"
        option-value="value"
        aria-label="Filter by camera"
        size="small"
        class="security-filter"
      />
      <Select
        v-model="severity"
        :options="SEVERITY_OPTIONS"
        option-label="label"
        option-value="value"
        aria-label="Filter by severity"
        size="small"
        class="security-filter"
      />
      <SelectButton
        v-model="period"
        :options="PERIOD_OPTIONS"
        option-label="label"
        option-value="value"
        :allow-empty="false"
        size="small"
        aria-label="Filter by period"
      />
      <span v-if="rows.length" class="security-toolbar-count">Showing {{ rows.length }} of {{ total }}</span>
    </div>

    <LoadingIndicator v-if="loading" />
    <Message v-else-if="failed" severity="error" :closable="false"> Failed to load the security timeline. </Message>
    <div v-else-if="!rows.length" class="security-empty">
      <AppIcon name="tab-security" class="security-empty-icon" />
      <template v-if="filtered">
        <h3>Nothing matches these filters</h3>
        <p>There are security events recorded — just none on the camera, severity or period selected above.</p>
        <Button label="Clear filters" size="small" severity="secondary" outlined @click="clearFilters" />
      </template>
      <template v-else>
        <h3>No security events yet</h3>
        <p>They appear once clips are analyzed with Enhanced Detection and Structured Security Analysis enabled.</p>
      </template>
    </div>

    <!-- The test id lives on this wrapper rather than on a row: it is the
         feed as a whole that tests scope their row queries to. -->
    <div v-else data-testid="security-timeline">
      <section v-for="day in days" :key="day.key" class="security-day">
        <h3 class="security-day-head">
          <span>{{ day.label }}</span>
          <span class="security-day-count">{{ day.rows.length }}</span>
        </h3>
        <ol class="security-day-rows">
          <li v-for="item in day.rows" :key="item.clip_id" class="security-row" :style="severityVars(item)">
            <div class="security-card" :class="{ 'is-open': expanded === item.clip_id }">
              <div class="security-row-body">
                <button
                  v-if="!thumbFailed[item.clip_id]"
                  type="button"
                  class="security-thumb"
                  :aria-label="`Open the clip from ${item.camera}`"
                  @click="openClip(item)"
                >
                  <img :src="clipThumbUrl(item.clip_id)" loading="lazy" alt="" @error="onThumbError(item.clip_id)" />
                  <span class="security-thumb-play" aria-hidden="true">▶</span>
                  <span v-if="item.start_offset > 0" class="security-thumb-at" aria-hidden="true">
                    {{ formatOffset(item.start_offset) }}
                  </span>
                </button>
                <div class="security-row-main">
                  <div class="security-row-head">
                    <span class="security-camera">{{ item.camera }}</span>
                    <span class="security-when">{{ formatWhen(item.clip_timestamp) }}</span>
                    <span class="security-risk" title="Code-computed risk score, 0-100"
                      >Risk {{ Math.round(item.risk_score) }}</span
                    >
                  </div>
                  <div class="security-row-tags">
                    <Tag :value="formatEventType(item.event_type)" :severity="severityTag(item.severity)" />
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
          </li>
        </ol>
      </section>
    </div>

    <div v-if="hasMore && !loading" class="security-more">
      <Button label="Load more" severity="secondary" outlined :loading="loadingMore" @click="loadMore" />
    </div>
  </div>
</template>

<style scoped>
.security-page {
  /* The same gutter every other tab uses (Storage, Vehicles, Live View,
     Security Feed, Biometrics) — without it the heading sat against the
     very top of the viewport and the timeline rail against the sidebar. */
  padding: 1.75rem;
  padding-bottom: 3rem;
  /* Wider than the 900px those pages settle on: a row here is a thumbnail,
     a heading, two lines of prose and a risk pill on one line, and 900px
     wraps the prose more than it needs to. */
  max-width: 1080px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — on a narrow viewport that pushes the whole
     page wider than the screen instead of wrapping its text. */
  min-width: 0;
  width: 100%;
}
.security-page h2 {
  font-size: 1.35rem;
  margin-bottom: 0.25rem;
}
.page-intro {
  color: var(--muted);
  font-size: 0.85rem;
  max-width: 78ch;
  margin-bottom: 1rem;
}

/* ── Filter bar ─────────────────────────────────────────── */
.security-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 0.55rem 0.6rem;
  margin-bottom: 1.1rem;
}
.security-filter {
  min-width: min(190px, 100%);
}
.security-toolbar-count {
  margin-left: auto;
  padding-right: 0.3rem;
  font-size: 0.78rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}

/* ── Day headings ───────────────────────────────────────── */
.security-day + .security-day {
  margin-top: 1.3rem;
}
.security-day-head {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  margin: 0 0 0.6rem;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--muted);
}
/* Rules the heading off across the rest of the width, which is what makes
   the groups read as sections of one timeline rather than as separate
   lists stacked on top of each other. */
.security-day-head::after {
  content: '';
  flex: 1;
  height: 1px;
  background: var(--border-strong);
}
.security-day-count {
  background: var(--card2);
  border: 1px solid var(--border);
  border-radius: 999px;
  padding: 0.05rem 0.45rem;
  font-size: 0.68rem;
  letter-spacing: 0;
  color: var(--text-dim);
}

/* ── The rail and its rows ──────────────────────────────── */
.security-day-rows {
  position: relative;
  list-style: none;
  margin: 0;
  padding: 0 0 0 26px;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}
/* The rail itself: one continuous line behind the day's dots. */
.security-day-rows::before {
  content: '';
  position: absolute;
  left: 6px;
  top: 6px;
  bottom: 6px;
  width: 2px;
  border-radius: 2px;
  background: var(--border-strong);
}
.security-row {
  position: relative;
  min-width: 0;
}
/* The dot, centred on the rail (the row's own left edge sits 26px in, and
   the dot is 12px wide, so -26px puts its centre back on the 6px rail). */
.security-row::before {
  content: '';
  position: absolute;
  left: -26px;
  top: 1.05rem;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: var(--sev);
  box-shadow:
    0 0 0 3px var(--bg),
    0 0 0 5px var(--sev-soft);
}

.security-card {
  display: flex;
  flex-direction: column;
  background: var(--card);
  border: 1px solid var(--border);
  border-left: 3px solid var(--sev);
  border-radius: var(--radius);
  box-shadow: var(--shadow-sm);
  padding: 0.75rem 0.85rem;
  transition:
    box-shadow 0.15s var(--ease),
    background 0.15s var(--ease);
}
.security-row:hover .security-card {
  box-shadow: var(--shadow);
}
.security-card.is-open {
  background: var(--card2);
}
.security-row-body {
  display: flex;
  gap: 0.85rem;
  align-items: flex-start;
}
.security-row-main {
  flex: 1;
  min-width: 0;
}

/* ── Thumbnail ──────────────────────────────────────────── */
.security-thumb {
  position: relative;
  flex: 0 0 auto;
  display: block;
  padding: 0;
  border: 0;
  background: var(--card2);
  cursor: pointer;
  line-height: 0;
  border-radius: 10px;
  overflow: hidden;
}
.security-thumb img {
  width: 148px;
  height: 83px;
  object-fit: cover;
  display: block;
  transition:
    transform 0.25s var(--ease),
    filter 0.15s var(--ease);
}
.security-thumb:hover img,
.security-thumb:focus-visible img {
  transform: scale(1.06);
  filter: brightness(0.7);
}
.security-thumb-play {
  position: absolute;
  inset: 0;
  margin: auto;
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: rgb(0 0 0 / 0.55);
  color: #fff;
  font-size: 0.72rem;
  line-height: 1;
  opacity: 0;
  transition: opacity 0.15s var(--ease);
}
.security-thumb:hover .security-thumb-play,
.security-thumb:focus-visible .security-thumb-play {
  opacity: 1;
}
/* Where in the clip the event was measured — the same second the "View
   clip at 0:18" button opens it at, shown on the picture it belongs to. */
.security-thumb-at {
  position: absolute;
  right: 4px;
  bottom: 4px;
  background: rgb(0 0 0 / 0.72);
  color: #fff;
  border-radius: 5px;
  padding: 0.05rem 0.3rem;
  font-size: 0.68rem;
  line-height: 1.5;
  font-variant-numeric: tabular-nums;
}

/* ── Row content ────────────────────────────────────────── */
.security-row-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 0.15rem 0.5rem;
}
.security-camera {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--text);
}
.security-when {
  font-size: 0.78rem;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
/* Tinted in the row's own band rather than coloured text: amber or grey
   type on the light theme's white card would not carry, while the same hue
   behind full-strength text does. */
.security-risk {
  margin-left: auto;
  background: var(--sev-soft);
  border: 1px solid var(--sev-line);
  border-radius: 999px;
  padding: 0.05rem 0.5rem;
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--text);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.security-row-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.4rem;
}
.security-disagree {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--warn);
  background: rgb(245 158 11 / 0.14);
  border: 1px solid rgb(245 158 11 / 0.36);
  border-radius: 999px;
  padding: 0.1rem 0.45rem;
  white-space: nowrap;
}
.security-detail-line {
  margin: 0.45rem 0 0;
  font-size: 0.875rem;
  line-height: 1.45;
  color: var(--text-dim);
}
/* The model's own words, set apart from the code-computed line above it —
   the two are different claims about the same clip and should not read as
   one paragraph. */
.security-ai-line {
  margin: 0.4rem 0 0;
  padding-left: 0.6rem;
  border-left: 2px solid var(--border-strong);
  font-size: 0.82rem;
  font-style: italic;
  line-height: 1.45;
  color: var(--muted);
}
.security-row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-top: 0.65rem;
}

/* ── Empty state / footer ───────────────────────────────── */
.security-empty {
  text-align: center;
  padding: 3rem 1.5rem;
  color: var(--muted);
}
.security-empty-icon {
  width: 2.6rem;
  height: 2.6rem;
  display: block;
  margin: 0 auto 0.7rem;
  opacity: 0.45;
}
.security-empty h3 {
  color: var(--text);
  font-size: 1rem;
  margin-bottom: 0.35rem;
}
.security-empty p {
  font-size: 0.85rem;
  max-width: 46ch;
  margin: 0 auto 0.8rem;
}
.security-more {
  display: flex;
  justify-content: center;
  margin-top: 1.1rem;
}

@media (max-width: 640px) {
  .security-day-rows {
    padding-left: 20px;
  }
  .security-row::before {
    left: -20px;
    width: 10px;
    height: 10px;
  }
  .security-day-rows::before {
    left: 5px;
  }
  .security-thumb img {
    width: 96px;
    height: 54px;
  }
  .security-card {
    padding: 0.65rem 0.7rem;
  }
  /* Nothing to push the pill against once the head row wraps — left-align
     it with the camera name instead of stranding it at the far edge. */
  .security-risk {
    margin-left: 0;
  }
  .security-toolbar-count {
    margin-left: 0;
  }
}
</style>
