<script setup lang="ts">
// The Assets tab: mark the things on each camera's view worth protecting —
// a front door, a parcel spot, a bike — so clip analysis looks at them
// first. Entirely optional: a camera with nothing marked is analyzed
// exactly as it always was, which the page says in so many words rather
// than leaving anyone to wonder whether an empty tab has switched something
// off.
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Message from 'primevue/message'
import Panel from 'primevue/panel'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import { getCameraConfigs } from '../../api/ai'
import { assetSnapshotUrl, deleteAsset, getAssetActivity, listAssets, updateAsset } from '../../api/assets'
import { describeApiError } from '../../api/client'
import type { AssetActivity, AssetLimits, ProtectedAsset } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import AssetCameraCard from './AssetCameraCard.vue'
import AssetEditorDialog from './AssetEditorDialog.vue'
import { ASSET_TYPES, assetTypeInfo, zoneColor } from './assetTypes'
import type { OverlayZone } from './assetZoneGeometry'

const toast = useToastStore()
const refresh = useRefreshStore()
const confirm = useConfirm()

const loading = ref(true)
const loadFailed = ref(false)
const cameras = ref<string[]>([])
const assets = ref<ProtectedAsset[]>([])
const limits = ref<AssetLimits>({ per_camera: 12, name: 48, description: 160 })
const analysisEnabled = ref(true)
const detectionEnabled = ref(true)
const activity = ref<AssetActivity[]>([])
const busy = ref(new Set<string>())

// Bumped after every save, so each camera's frame is fetched afresh rather
// than the browser keeping the one it cached before a redraw.
const frameVersion = ref(0)

const editor = ref<{ camera: string; asset: ProtectedAsset | null } | null>(null)

// The newest load wins, and a refresh never starts over an open editor:
// the camera list shifting under someone mid-drawing is worse than showing
// them a list a few seconds stale.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  loadFailed.value = false
  try {
    const [configs, data] = await Promise.all([getCameraConfigs(), listAssets()])
    // Activity is decoration: a failure to count events must not cost the
    // page the assets themselves.
    const recent = await getAssetActivity().catch(() => null)
    if (seq !== requestSeq) return
    cameras.value = configs.map((c) => c.camera)
    assets.value = data.assets
    limits.value = data.limits
    analysisEnabled.value = data.analysis_enabled
    detectionEnabled.value = data.detection_enabled
    activity.value = recent?.activity ?? []
  } catch {
    if (seq === requestSeq) loadFailed.value = true
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

onMounted(load)
watch(
  () => refresh.tick,
  () => {
    if (!editor.value) void load()
  },
)

const lowerCameras = computed(() => new Set(cameras.value.map((c) => c.toLowerCase())))

function assetsOn(camera: string): ProtectedAsset[] {
  const key = camera.toLowerCase()
  return assets.value.filter((a) => a.camera.toLowerCase() === key)
}

/** Assets left on a camera that is no longer on the account — renamed
 * before this add-on saw it happen, or removed. Nothing will analyze them,
 * and they can't be redrawn, but they can be cleared away. */
const orphaned = computed(() => assets.value.filter((a) => !lowerCameras.value.has(a.camera.toLowerCase())))

function activityOn(camera: string): Record<string, AssetActivity> {
  const key = camera.toLowerCase()
  return Object.fromEntries(activity.value.filter((a) => a.camera.toLowerCase() === key).map((a) => [a.asset_name, a]))
}

function snapshotFor(camera: string): string {
  return `${assetSnapshotUrl(camera)}?v=${frameVersion.value}`
}

const watched = computed(() => assets.value.filter((a) => a.enabled && lowerCameras.value.has(a.camera.toLowerCase())))
const watchedCameras = computed(() => new Set(watched.value.map((a) => a.camera.toLowerCase())).size)

// -- the editor --------------------------------------------------------

const editorCamera = computed(() => editor.value?.camera ?? '')
const editorAssets = computed(() => (editor.value ? assetsOn(editor.value.camera) : []))
const editorOthers = computed<OverlayZone[]>(() =>
  editorAssets.value
    .map((asset, index) => ({ asset, index }))
    .filter(({ asset }) => asset.id !== editor.value?.asset?.id)
    .map(({ asset, index }) => ({
      id: asset.id,
      name: asset.name,
      zone: asset.zone,
      color: zoneColor(index),
      muted: !asset.enabled,
    })),
)
const editorColor = computed(() => {
  const current = editor.value?.asset
  const index = current ? editorAssets.value.findIndex((a) => a.id === current.id) : editorAssets.value.length
  return zoneColor(index)
})

function openEditor(camera: string, asset: ProtectedAsset | null = null) {
  editor.value = { camera, asset }
}

function onEditorVisible(visible: boolean) {
  if (!visible) editor.value = null
}

function onSaved(saved: ProtectedAsset) {
  const index = assets.value.findIndex((a) => a.id === saved.id)
  if (index >= 0) assets.value.splice(index, 1, saved)
  else assets.value.push(saved)
  frameVersion.value++
  toast.show(index >= 0 ? `Saved “${saved.name}”` : `“${saved.name}” is now protected`)
}

// -- row actions -------------------------------------------------------

async function toggle(asset: ProtectedAsset, enabled: boolean) {
  busy.value = new Set(busy.value).add(asset.id)
  asset.enabled = enabled
  try {
    const { asset: saved } = await updateAsset(asset.id, { enabled })
    Object.assign(asset, saved)
    toast.show(enabled ? `Watching “${asset.name}” again` : `Stopped watching “${asset.name}”`)
  } catch (err) {
    asset.enabled = !enabled
    toast.show(describeApiError(err, `Couldn't update “${asset.name}”`), true)
  } finally {
    const next = new Set(busy.value)
    next.delete(asset.id)
    busy.value = next
  }
}

async function remove(asset: ProtectedAsset) {
  const ok = await confirm(
    `Stop protecting “${asset.name}” on ${asset.camera} and remove its zone? Clips from ${asset.camera} will be analyzed without it.`,
    'Remove asset?',
  )
  if (!ok) return
  try {
    await deleteAsset(asset.id)
    assets.value = assets.value.filter((a) => a.id !== asset.id)
    toast.show(`Removed “${asset.name}”`)
  } catch (err) {
    toast.show(describeApiError(err, `Couldn't remove “${asset.name}”`), true)
  }
}
</script>

<template>
  <div class="assets-page">
    <h2>Assets</h2>
    <p class="page-intro">
      Mark the things you most want protected on each camera's view — a front door, a spot where parcels are left, a
      bike, a gate. Clips from that camera are then analyzed with them in mind: the AI is told what and where they are,
      the frames it's shown are chosen for activity at them, and — with a second-opinion model set up — a clip it calls
      quiet is double-checked. Cameras with nothing marked are analyzed exactly as before.
    </p>

    <Message v-if="!loading && !analysisEnabled" severity="warn" size="small" :closable="false" class="status-banner">
      AI analysis isn't set up, so nothing is reading these assets yet. Choose an AI provider in the add-on's
      Configuration tab.
    </Message>
    <Message
      v-else-if="!loading && !detectionEnabled && assets.length"
      severity="info"
      size="small"
      :closable="false"
      class="status-banner"
    >
      The AI is already told about these assets. Turn on <strong>Enhanced Detection</strong> in the add-on's
      Configuration tab to add the checks that run on every clip: someone going into an asset's area, handling it, or
      the asset looking different afterwards.
    </Message>
    <Message v-if="!loading && watched.length" severity="success" size="small" :closable="false" class="status-banner">
      Protecting {{ watched.length }} {{ watched.length === 1 ? 'asset' : 'assets' }} on {{ watchedCameras }}
      {{ watchedCameras === 1 ? 'camera' : 'cameras' }}.
    </Message>

    <Panel
      toggleable
      collapsed
      class="how-panel"
      :toggle-button-props="{ ariaLabel: 'Show or hide how each kind of asset is watched' }"
    >
      <template #header>
        <span class="how-title"><i class="pi pi-shield" aria-hidden="true" /> How each kind of asset is watched</span>
      </template>
      <ul class="how-list">
        <li v-for="info in ASSET_TYPES" :key="info.value">
          <i :class="info.icon" aria-hidden="true" />
          <span
            ><strong>{{ info.label }}.</strong> {{ info.watchedFor }}</span
          >
        </li>
      </ul>
      <p class="muted-note">
        With face recognition on, a clip of an approved household member (see the Biometrics tab) is treated as routine
        at any of these.
      </p>
    </Panel>

    <div v-if="loading" class="camera-list">
      <Skeleton v-for="n in 2" :key="n" height="9rem" />
    </div>
    <Message v-else-if="loadFailed" severity="error" size="small" :closable="false">
      Couldn't load your assets.
      <Button label="Try again" size="small" text @click="load" />
    </Message>
    <div v-else-if="!cameras.length && !assets.length" class="empty-state">
      <i class="pi pi-camera" aria-hidden="true" />
      <p class="empty-title">No cameras found</p>
      <p class="muted-note">Assets are marked on a camera's frame. Download at least one clip first.</p>
    </div>
    <div v-else class="camera-list">
      <AssetCameraCard
        v-for="camera in cameras"
        :key="camera"
        :camera="camera"
        :assets="assetsOn(camera)"
        :activity="activityOn(camera)"
        :snapshot-url="snapshotFor(camera)"
        :per-camera-limit="limits.per_camera"
        :busy="busy"
        @add="openEditor(camera)"
        @edit="openEditor(camera, $event)"
        @remove="remove"
        @toggle="toggle"
      />

      <div v-if="orphaned.length" class="orphaned">
        <h3 class="orphaned-title">
          Cameras no longer on your account
          <Tag :value="`${orphaned.length}`" severity="secondary" />
        </h3>
        <p class="muted-note">
          These were marked on cameras Blink no longer reports under that name, so nothing analyzes them.
        </p>
        <ul class="orphaned-list">
          <li v-for="asset in orphaned" :key="asset.id">
            <i :class="assetTypeInfo(asset.asset_type).icon" aria-hidden="true" />
            <span class="orphaned-name">{{ asset.name }}</span>
            <span class="muted-note">on {{ asset.camera }}</span>
            <Button
              icon="pi pi-trash"
              text
              rounded
              severity="danger"
              :aria-label="`Remove ${asset.name}`"
              @click="remove(asset)"
            />
          </li>
        </ul>
      </div>
    </div>

    <AssetEditorDialog
      :visible="editor !== null"
      :camera="editorCamera"
      :asset="editor?.asset ?? null"
      :others="editorOthers"
      :color="editorColor"
      :has-reference-frame="editorAssets.length > 0"
      :snapshot-url="snapshotFor(editorCamera)"
      :limits="limits"
      @update:visible="onEditorVisible"
      @saved="onSaved"
    />
  </div>
</template>

<style scoped>
.assets-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  max-width: 1000px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — the same fix every settings page has. */
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.status-banner {
  margin-bottom: 0.9rem;
}

.how-panel {
  margin-bottom: 1.25rem;
}

.how-title {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
  font-weight: 600;
}

.how-list {
  list-style: none;
  margin: 0 0 0.75rem;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(280px, 100%), 1fr));
  gap: 0.6rem 1.25rem;
  font-size: 0.85rem;
}

.how-list li {
  display: flex;
  gap: 0.55rem;
  align-items: baseline;
}

.how-list .pi {
  color: var(--accent);
}

.camera-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2rem 1rem;
  text-align: center;
}

.empty-state .pi {
  font-size: 1.75rem;
  color: var(--muted);
}

.empty-title {
  margin: 0;
  font-weight: 700;
}

.orphaned {
  padding: 1rem 1.1rem;
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
}

.orphaned-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0 0 0.35rem;
  font-size: 1rem;
}

.orphaned-list {
  list-style: none;
  margin: 0.6rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.orphaned-list li {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.orphaned-name {
  font-weight: 600;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
  margin: 0;
}

@media (max-width: 600px) {
  .assets-page {
    padding: 1rem;
  }
  /* Cards inside the page: at full padding a phone's width leaves too
     little room for each asset row's switch and buttons. */
  .camera-list :deep(.p-card-body) {
    padding: 1rem;
  }
}
</style>
