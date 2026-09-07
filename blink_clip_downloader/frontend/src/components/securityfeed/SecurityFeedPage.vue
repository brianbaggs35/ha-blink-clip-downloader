<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import InputNumber from 'primevue/inputnumber'
import MultiSelect from 'primevue/multiselect'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
import SelectButton from 'primevue/selectbutton'
import {
  getSecurityFeedCameras,
  getSecurityFeedSettings,
  saveSecurityFeedSettings,
  securityFeedSnapshotUrl,
} from '../../api/securityFeed'
import type { SecurityFeedSettings } from '../../api/types'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// {label, value} pairs, not raw numbers - PrimeVue's SelectButton passes
// each option through to a ToggleButton whose onLabel/offLabel props
// require strings, so raw numeric options trigger a prop-type warning
// even though they render fine.
// Capped at 3: past that, a tile shrinks too small to make out what a
// snapshot actually shows. 1-2 is the comfortable range; 3 fits but is
// the "cramped, not ideal" upper bound, not a recommended default.
const COLUMN_OPTIONS = [1, 2, 3].map((n) => ({ label: String(n), value: n }))

const toast = useToastStore()
const refresh = useRefreshStore()

const loading = ref(true)
const loadError = ref(false)
const allCameras = ref<string[]>([])
const settings = ref<SecurityFeedSettings>({ cameras: [], columns: 2, refresh_seconds: 15 })
const saving = ref(false)

// Draft state edited by the customize panel, only applied to `settings`
// (and therefore to the live grid/refresh timer) on Save — changing which
// cameras/columns to show shouldn't take effect keystroke-by-keystroke.
const draftCameras = ref<string[]>([])
const draftColumns = ref(2)
const draftRefreshSeconds = ref(15)
let loadedDraftSignature = ''

// Bumped on every refresh tick and appended as a cache-busting query
// param on every tile's <img> src - without it the browser would just
// keep showing its cached copy of the same URL forever. This never
// triggers a new Blink snapshot; it only asks our own backend for
// whatever camera.image_from_cache already holds (see
// BlinkDownloader.get_camera_snapshot's docstring).
const refreshTick = ref(Date.now())
let refreshTimer: ReturnType<typeof setInterval> | undefined
let loadSeq = 0
const tileErrors = ref<Record<string, boolean>>({})

const displayedCameras = computed(() => {
  const selected = settings.value.cameras.filter((c) => allCameras.value.includes(c))
  return selected.length ? selected : allCameras.value
})

function tileSrc(camera: string): string {
  return `${securityFeedSnapshotUrl(camera)}?t=${refreshTick.value}`
}

function onTileError(camera: string) {
  tileErrors.value = { ...tileErrors.value, [camera]: true }
}

function onTileLoad(camera: string) {
  if (!tileErrors.value[camera]) return
  const next = { ...tileErrors.value }
  delete next[camera]
  tileErrors.value = next
}

function stopRefreshTimer() {
  clearInterval(refreshTimer)
  refreshTimer = undefined
}

function startRefreshTimer() {
  stopRefreshTimer()
  refreshTimer = setInterval(() => {
    refreshTick.value = Date.now()
  }, settings.value.refresh_seconds * 1000)
}

watch(
  () => settings.value.refresh_seconds,
  () => startRefreshTimer(),
)

async function load() {
  const seq = ++loadSeq
  loading.value = true
  loadError.value = false
  try {
    const [camerasRes, settingsRes] = await Promise.all([getSecurityFeedCameras(), getSecurityFeedSettings()])
    if (seq !== loadSeq) return
    allCameras.value = camerasRes.cameras
    settings.value = settingsRes
    draftCameras.value = [...settingsRes.cameras]
    draftColumns.value = settingsRes.columns
    draftRefreshSeconds.value = settingsRes.refresh_seconds
    loadedDraftSignature = JSON.stringify(settingsRes)
  } catch {
    if (seq === loadSeq) loadError.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

async function saveSettings() {
  saving.value = true
  try {
    const saved = await saveSecurityFeedSettings({
      cameras: draftCameras.value,
      columns: draftColumns.value,
      refresh_seconds: draftRefreshSeconds.value,
    })
    settings.value = saved
    loadedDraftSignature = JSON.stringify(saved)
    tileErrors.value = {}
    refreshTick.value = Date.now()
    toast.show('Security Feed settings saved')
  } catch {
    toast.show('Could not save Security Feed settings', true)
  } finally {
    saving.value = false
  }
}

onMounted(async () => {
  await load()
  startRefreshTimer()
})
onUnmounted(stopRefreshTimer)
watch(
  () => refresh.tick,
  () => {
    const draftSignature = JSON.stringify({
      cameras: draftCameras.value,
      columns: draftColumns.value,
      refresh_seconds: draftRefreshSeconds.value,
    })
    if (draftSignature === loadedDraftSignature) void load()
  },
)
</script>

<template>
  <div class="secfeed-page">
    <h2>Security Feed</h2>
    <p class="page-intro">
      A grid of near-live snapshots from your Blink cameras. Each tile shows whatever image the add-on's own poll cycle
      most recently cached — no extra load on Blink's API beyond what this add-on already does.
    </p>

    <Message severity="info" size="small" :closable="false" class="secfeed-info-banner">
      Tiles only change when Blink itself records new motion on that camera — this isn't a continuous live feed, and
      "Refresh every" just controls how often we re-check for a newer cached snapshot, not how often a new one is taken.
      A quiet camera can show the same image for a while; that's expected, not stuck.
    </Message>

    <div v-if="loading" style="padding: 1rem"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" size="small" :closable="false">
      Failed to load the Security Feed. Try refreshing the page.
    </Message>
    <template v-else>
      <Panel header="Customize" toggleable collapsed class="secfeed-customize">
        <div class="secfeed-controls">
          <div class="secfeed-field">
            <label for="secfeed-cameras" class="field-label">Cameras</label>
            <MultiSelect
              id="secfeed-cameras"
              v-model="draftCameras"
              :options="allCameras"
              placeholder="All cameras"
              display="chip"
              class="secfeed-cameras-select"
              filter
            />
          </div>
          <div class="secfeed-field">
            <span class="field-label">Tiles per row</span>
            <SelectButton
              v-model="draftColumns"
              :options="COLUMN_OPTIONS"
              option-label="label"
              option-value="value"
              :allow-empty="false"
              aria-label="Tiles per row"
            />
          </div>
          <div class="secfeed-field">
            <label for="secfeed-refresh" class="field-label">Refresh every (seconds)</label>
            <InputNumber
              id="secfeed-refresh"
              v-model="draftRefreshSeconds"
              :min="5"
              :max="300"
              :step="5"
              show-buttons
              button-layout="horizontal"
              class="secfeed-refresh-input"
            />
          </div>
          <Button
            size="small"
            class="secfeed-save-btn"
            :disabled="saving"
            :loading="saving"
            label="Save"
            @click="saveSettings"
          />
        </div>
      </Panel>

      <Message v-if="!displayedCameras.length" severity="info" size="small" :closable="false">
        No cameras found — make sure Blink is connected and your account has at least one camera.
      </Message>
      <div v-else class="secfeed-grid" :style="{ '--secfeed-columns': settings.columns }">
        <div v-for="camera in displayedCameras" :key="camera" class="secfeed-tile">
          <div class="secfeed-tile-image-wrap">
            <img
              :src="tileSrc(camera)"
              :alt="`${camera} snapshot`"
              class="secfeed-tile-image"
              :class="{ 'secfeed-tile-image-hidden': tileErrors[camera] }"
              @error="onTileError(camera)"
              @load="onTileLoad(camera)"
            />
            <div v-if="tileErrors[camera]" class="secfeed-tile-placeholder muted-note">No snapshot available yet</div>
          </div>
          <div class="secfeed-tile-name">{{ camera }}</div>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.secfeed-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.secfeed-info-banner {
  margin-bottom: 1rem;
}

.secfeed-customize {
  margin-bottom: 1rem;
}

.secfeed-controls {
  display: flex;
  align-items: flex-end;
  gap: 1rem;
  flex-wrap: wrap;
}

.secfeed-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.field-label {
  font-size: 0.82rem;
  color: var(--muted);
}

.secfeed-cameras-select {
  min-width: min(280px, 100%);
}

.secfeed-refresh-input {
  width: 11rem;
}

.secfeed-save-btn {
  /* Pins Save to the panel's right edge instead of sitting bunched up
     against whichever field happens to wrap onto the last line. */
  margin-left: auto;
}

.secfeed-grid {
  display: grid;
  /* Fills whatever width the page actually has - grows automatically if
     the surrounding layout ever gives this page more room (e.g. a
     collapsed nav sidebar), no fixed max-width capping it. */
  grid-template-columns: repeat(var(--secfeed-columns, 3), minmax(0, 1fr));
  gap: 1rem;
}

.secfeed-tile {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.secfeed-tile-image-wrap {
  position: relative;
  aspect-ratio: 16 / 9;
  background: #000;
  overflow: hidden;
}

.secfeed-tile-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.secfeed-tile-image-hidden {
  display: none;
}

.secfeed-tile-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  padding: 0.5rem;
}

.secfeed-tile-name {
  padding: 0.5rem 0.75rem;
  font-weight: 600;
  font-size: 0.85rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}

@media (max-width: 600px) {
  .secfeed-page {
    padding: 1rem;
  }
  .secfeed-grid {
    /* Whatever column count is configured for desktop, a phone-width
       screen can't usefully honor it - auto-fill/minmax (the same
       pattern .clip-grid already uses) lets the grid decide for itself
       how many tiles fit at a readable size instead of cramming a fixed
       count down to unreadable slivers. */
    grid-template-columns: repeat(auto-fill, minmax(min(150px, 100%), 1fr));
  }
}
</style>
