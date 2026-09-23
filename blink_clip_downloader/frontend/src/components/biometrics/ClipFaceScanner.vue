<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import ProgressBar from 'primevue/progressbar'
import Select from 'primevue/select'
import Skeleton from 'primevue/skeleton'
import { ApiError, describeApiError } from '../../api/client'
import { clipThumbUrl, getCameras, listClips } from '../../api/clips'
import { fmtDur } from '../../api/constants'
import { scanClipForFaces } from '../../api/faces'
import type { CameraStat, ClipListItem, FaceCandidate } from '../../api/types'
import { useRefreshStore } from '../../stores/refresh'
import AppIcon from '../icons/AppIcon.vue'
import { collapseDuplicateClips } from './found'

const props = defineProps<{ available: boolean }>()
const emit = defineEmits<{
  found: [faces: FaceCandidate[], clip: ClipListItem]
  'scanning-change': [scanning: boolean]
}>()

const refresh = useRefreshStore()

const ALL_CAMERAS = ''
const LOOKBACK_OPTIONS = [
  { label: 'Last 6 hours', value: 6 },
  { label: 'Last 24 hours', value: 24 },
  { label: 'Last 3 days', value: 72 },
  { label: 'Last 7 days', value: 24 * 7 },
  { label: 'Last 30 days', value: 24 * 30 },
]
const CLIPS_PAGE_SIZE = 24

const cameras = ref<CameraStat[]>([])
const camera = ref(ALL_CAMERAS)
const lookbackHours = ref(24)
const cameraOptions = computed(() => [
  { label: 'All cameras', value: ALL_CAMERAS },
  ...cameras.value.map((c) => ({ label: c.camera, value: c.camera })),
])

// Every clip fetched so far for the current filters (paged in with "Load
// more"), before duplicate copies are collapsed for display.
const fetched = ref<ClipListItem[]>([])
const loadingClips = ref(false)
const clipsFailed = ref(false)
const hasMore = ref(false)
const thumbFailed = ref<Record<string, boolean>>({})
const listed = computed(() => collapseDuplicateClips(fetched.value))

interface ClipScan {
  status: 'queued' | 'scanning' | 'done' | 'error'
  faces: number
  error?: string
}
const scans = ref<Record<string, ClipScan>>({})
const queue: ClipListItem[] = []
const running = ref(false)
const stopRequested = ref(false)
const progress = ref({ done: 0, total: 0 })
let alive = true

const unscanned = computed(() => listed.value.clips.filter((clip) => !scans.value[clip.id]))
// While a run is going, what is left is queued rather than scanned.
const scanAllLabel = computed(() => {
  const count = unscanned.value.length
  if (count) return `Find faces in ${count} clip${count === 1 ? '' : 's'}`
  return running.value ? 'Scanning…' : 'All shown clips scanned'
})

async function loadCameras() {
  try {
    cameras.value = await getCameras()
    if (camera.value && !cameras.value.some((c) => c.camera === camera.value)) camera.value = ALL_CAMERAS
  } catch {
    // Transient — the camera list stays at its last known state.
  }
}

let clipsSeq = 0
async function loadClips(append = false) {
  const seq = ++clipsSeq
  loadingClips.value = true
  clipsFailed.value = false
  if (!append) fetched.value = []
  try {
    const page = await listClips({
      camera: camera.value || undefined,
      since: new Date(Date.now() - lookbackHours.value * 3_600_000).toISOString(),
      limit: CLIPS_PAGE_SIZE,
      offset: append ? fetched.value.length : 0,
      sort: 'newest',
    })
    if (seq !== clipsSeq) return
    // A clip arriving between pages shifts the offsets by one; skip what we
    // already hold rather than list it twice.
    const known = new Set(fetched.value.map((c) => c.id))
    fetched.value = [...fetched.value, ...page.filter((c) => !known.has(c.id))]
    hasMore.value = page.length === CLIPS_PAGE_SIZE
  } catch {
    if (seq === clipsSeq) clipsFailed.value = true
  } finally {
    if (seq === clipsSeq) loadingClips.value = false
  }
}

loadCameras() // NOSONAR — fire-and-forget; the list renders its own loading state
loadClips() // NOSONAR
watch([camera, lookbackHours], () => loadClips())
watch(
  () => refresh.tick,
  () => {
    loadCameras()
    loadClips()
  },
)

onUnmounted(() => {
  alive = false
})

/** Queue *clips* for scanning, skipping any already scanned or queued. */
function scan(clips: ClipListItem[]) {
  const fresh = clips.filter((clip) => !scans.value[clip.id])
  if (!fresh.length) return
  for (const clip of fresh) scans.value[clip.id] = { status: 'queued', faces: 0 }
  queue.push(...fresh)
  progress.value = running.value
    ? { done: progress.value.done, total: progress.value.total + fresh.length }
    : { done: 0, total: fresh.length }
  if (!running.value) void runQueue()
}

// One clip per request, one request at a time: a scan is seconds of CPU on
// a Raspberry Pi, and the server shares its vision models with clip
// analysis — firing a dozen at once would only make everything wait.
async function runQueue() {
  running.value = true
  stopRequested.value = false
  emit('scanning-change', true)
  while (queue.length && alive && !stopRequested.value) {
    const clip = queue.shift()!
    scans.value[clip.id] = { status: 'scanning', faces: 0 }
    try {
      const result = await scanClipForFaces(clip.id)
      if (!alive) return
      scans.value[clip.id] = result.error
        ? { status: 'error', faces: 0, error: result.error }
        : { status: 'done', faces: result.faces.length }
      if (result.faces.length) emit('found', result.faces, clip)
    } catch (e) {
      if (!alive) return
      scans.value[clip.id] = { status: 'error', faces: 0, error: scanFailure(e) }
    }
    progress.value = { ...progress.value, done: progress.value.done + 1 }
  }
  // Anything still queued after a stop goes back to unscanned, so it can
  // be picked again rather than sitting "queued" forever.
  for (const clip of queue.splice(0)) delete scans.value[clip.id]
  running.value = false
  emit('scanning-change', false)
}

// A clip can be deleted — retention, a storage cleanup, someone in the
// Library tab — between being listed here and its turn to be scanned.
function scanFailure(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return 'This clip is no longer in the library'
  return describeApiError(error, 'Scan failed — check your connection and try again')
}

function stop() {
  stopRequested.value = true
}

function clipTime(ts: string): string {
  return new Date(ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
}

function statusText(clip: ClipListItem): string {
  const s = scans.value[clip.id]
  if (!s) return 'Scan'
  if (s.status === 'queued') return 'Queued'
  if (s.status === 'scanning') return 'Scanning…'
  if (s.status === 'error') return 'Failed — hover for why'
  if (!s.faces) return 'No faces'
  return `${s.faces} face${s.faces === 1 ? '' : 's'}`
}

defineExpose({ scan })
</script>

<template>
  <div class="clip-face-scanner">
    <div class="scanner-controls">
      <div class="scanner-field">
        <!-- aria-labelledby as well as the label's for: the for makes clicking
             the label open the Select, and aria-labelledby is the association
             an HTML-level checker can see, since it reads <Select> as a native
             <select> and knows nothing of PrimeVue's input-id. -->
        <label id="biometrics-camera-label" for="biometrics-camera-select" class="field-label">Camera</label>
        <Select
          v-model="camera"
          input-id="biometrics-camera-select"
          aria-labelledby="biometrics-camera-label"
          size="small"
          :options="cameraOptions"
          option-label="label"
          option-value="value"
          class="scanner-select"
        />
      </div>
      <div class="scanner-field">
        <label id="biometrics-lookback-label" for="biometrics-lookback-select" class="field-label">Clips from</label>
        <Select
          v-model="lookbackHours"
          input-id="biometrics-lookback-select"
          aria-labelledby="biometrics-lookback-label"
          size="small"
          :options="LOOKBACK_OPTIONS"
          option-label="label"
          option-value="value"
          class="scanner-select"
        />
      </div>
      <Button
        class="scan-all-btn"
        size="small"
        icon="pi pi-search"
        :label="scanAllLabel"
        :disabled="!props.available || !unscanned.length"
        @click="scan(unscanned)"
      />
    </div>

    <div v-if="running" class="scan-progress">
      <ProgressBar :value="Math.round((progress.done / progress.total) * 100)" :show-value="false" />
      <!-- <output> is itself a polite live region: the progress is announced
           as it changes, without the bar or the Stop button being read out. -->
      <output class="field-label">
        {{
          stopRequested
            ? 'Stopping after this clip…'
            : `Scanning clip ${Math.min(progress.done + 1, progress.total)} of ${progress.total}…`
        }}
      </output>
      <Button label="Stop" size="small" text severity="secondary" :disabled="stopRequested" @click="stop" />
    </div>

    <div v-if="loadingClips && !listed.clips.length" class="clip-grid">
      <Skeleton v-for="n in 6" :key="n" height="5.5rem" />
    </div>
    <Message v-else-if="clipsFailed && !listed.clips.length" severity="error" size="small" :closable="false">
      Couldn't load clips.
      <Button label="Try again" size="small" text @click="loadClips()" />
    </Message>
    <Message v-else-if="!listed.clips.length" severity="secondary" size="small" :closable="false">
      No clips {{ camera ? `from ${camera} ` : '' }}in that time range — try a longer one.
    </Message>
    <template v-else>
      <p class="field-label">
        Pick the clips someone walks toward the camera in, or scan them all. Only frames with a face in them come back.
      </p>
      <div class="clip-grid">
        <button
          v-for="clip in listed.clips"
          :key="clip.id"
          type="button"
          class="clip-tile"
          :class="[`clip-tile--${scans[clip.id]?.status ?? 'new'}`]"
          :disabled="!props.available || !!scans[clip.id]"
          :title="scans[clip.id]?.error ?? ''"
          :aria-label="`${statusText(clip)}: ${clip.camera}, ${clipTime(clip.timestamp)}`"
          @click="scan([clip])"
        >
          <img
            v-if="!thumbFailed[clip.id]"
            :src="clipThumbUrl(clip.id)"
            alt=""
            loading="lazy"
            @error="thumbFailed = { ...thumbFailed, [clip.id]: true }"
          />
          <div v-else class="clip-tile-no-thumb"><AppIcon name="no-thumb" /></div>
          <span class="clip-tile-status">{{ statusText(clip) }}</span>
          <span class="clip-tile-meta">
            <span v-if="!camera" class="clip-tile-camera">{{ clip.camera }}</span>
            <span>{{ clipTime(clip.timestamp) }}</span>
            <span v-if="clip.duration">· {{ fmtDur(clip.duration) }}</span>
            <span v-if="clip.face_recognized" title="Someone enrolled was already recognized in this clip">· 👤</span>
          </span>
        </button>
      </div>
      <div class="scanner-footer">
        <Button
          v-if="clipsFailed"
          label="Couldn't load more — try again"
          size="small"
          text
          severity="danger"
          @click="loadClips(true)"
        />
        <Button
          v-else-if="hasMore"
          label="Load more clips"
          size="small"
          text
          :loading="loadingClips"
          @click="loadClips(true)"
        />
        <span v-if="listed.hidden" class="field-label">
          {{ listed.hidden }} duplicate cop{{ listed.hidden === 1 ? 'y' : 'ies' }} hidden — the same event saved again
          to the Sync Module's USB storage.
        </span>
      </div>
    </template>
  </div>
</template>

<style scoped>
.clip-face-scanner {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.scanner-controls {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  gap: 0.75rem;
}

.scanner-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.scanner-select {
  min-width: min(200px, 100%);
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
  margin: 0;
}

.scan-progress {
  display: grid;
  grid-template-columns: 1fr auto;
  align-items: center;
  gap: 0.35rem 0.75rem;
}

.scan-progress :deep(.p-progressbar) {
  grid-column: 1 / -1;
  height: 0.4rem;
}

.clip-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(120px, 100%), 1fr));
  gap: 0.6rem;
}

.clip-tile {
  position: relative;
  display: flex;
  flex-direction: column;
  padding: 0;
  border: 2px solid transparent;
  border-radius: var(--radius-sm, 10px);
  overflow: hidden;
  background: var(--card2);
  color: inherit;
  text-align: left;
  cursor: pointer;
  font: inherit;
}

.clip-tile:disabled {
  cursor: default;
}

.clip-tile:not(:disabled):hover,
.clip-tile:focus-visible {
  border-color: var(--accent);
}

.clip-tile img,
.clip-tile-no-thumb {
  width: 100%;
  aspect-ratio: 16 / 9;
  object-fit: cover;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--muted);
}

.clip-tile-status {
  position: absolute;
  top: 0.35rem;
  right: 0.35rem;
  padding: 0.1rem 0.45rem;
  border-radius: 999px;
  font-size: 0.7rem;
  font-weight: 600;
  background: rgba(0, 0, 0, 0.65);
  color: #fff;
}

.clip-tile--done .clip-tile-status {
  background: var(--accent);
}

.clip-tile--error .clip-tile-status {
  background: var(--danger);
}

.clip-tile--done,
.clip-tile--error {
  opacity: 0.8;
}

.clip-tile-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.25rem;
  padding: 0.35rem 0.5rem;
  font-size: 0.72rem;
  color: var(--text-dim);
}

.clip-tile-camera {
  font-weight: 600;
  width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.scanner-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}
</style>
