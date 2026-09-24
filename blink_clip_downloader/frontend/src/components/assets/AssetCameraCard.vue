<script setup lang="ts">
// One camera's marked assets: its reference frame with every zone drawn on
// it, beside the list of what those zones are. Hovering either side lights
// up the other, so which outline is which is never a guess. A camera with
// nothing marked stays a single compact line — a long list of cameras must
// not become a long scroll of pictures nobody asked to see.
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import type { AssetActivity, ProtectedAsset } from '../../api/types'
import AssetZoneOverlay from './AssetZoneOverlay.vue'
import { assetTypeInfo, zoneColor } from './assetTypes'
import { type OverlayZone, zoneRegion } from './assetZoneGeometry'

const props = defineProps<{
  camera: string
  assets: ProtectedAsset[]
  /** Recent activity per asset name on this camera. */
  activity: Record<string, AssetActivity>
  snapshotUrl: string
  perCameraLimit: number
  /** Asset ids with a save in flight, whose switches wait for it. */
  busy: Set<string>
}>()
const emit = defineEmits<{
  add: []
  edit: [asset: ProtectedAsset]
  remove: [asset: ProtectedAsset]
  toggle: [asset: ProtectedAsset, enabled: boolean]
}>()

const highlighted = ref<string | null>(null)
const frameFailed = ref(false)

const zones = computed<OverlayZone[]>(() =>
  props.assets.map((asset, index) => ({
    id: asset.id,
    name: asset.name,
    zone: asset.zone,
    color: zoneColor(index),
    muted: !asset.enabled,
  })),
)

const watchedCount = computed(() => props.assets.filter((a) => a.enabled).length)
const atLimit = computed(() => props.assets.length >= props.perCameraLimit)

const SEVERITY_TAGS = {
  critical: 'danger',
  suspicious: 'warn',
  noteworthy: 'info',
  routine: 'secondary',
} as const

/** Each asset's recent-activity tag, by asset id; absent when it's been quiet. */
const activityTags = computed(() => {
  const tags: Record<string, { label: string; severity: string; title: string }> = {}
  for (const asset of props.assets) {
    const row = props.activity[asset.name]
    if (!row) continue
    tags[asset.id] = {
      label: `${row.clips} ${row.clips === 1 ? 'clip' : 'clips'} this week`,
      severity: SEVERITY_TAGS[row.top_severity] ?? 'secondary',
      title: `Most serious: ${row.top_severity}. Latest: ${new Date(row.last_seen).toLocaleString()}`,
    }
  }
  return tags
})

function focusRow(id: string) {
  highlighted.value = id
  document.getElementById(`asset-row-${id}`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
}
</script>

<template>
  <Card class="asset-camera-card" :data-camera="camera">
    <template #title>
      <div class="card-title">
        <h3 class="camera-name">📷 {{ camera }}</h3>
        <Tag
          v-if="assets.length"
          :value="`${watchedCount} of ${assets.length} watched`"
          :severity="watchedCount ? 'success' : 'secondary'"
        />
      </div>
    </template>
    <template #content>
      <div v-if="!assets.length" class="empty-camera">
        <span class="muted-note">Nothing marked on this camera. Clips from it are analyzed as usual.</span>
        <Button label="Mark an asset" icon="pi pi-plus" size="small" outlined @click="emit('add')" />
      </div>

      <div v-else class="camera-layout">
        <div class="camera-frame">
          <img
            v-if="!frameFailed"
            :src="snapshotUrl"
            :alt="`${camera} with its marked assets`"
            class="camera-frame-image"
            @error="frameFailed = true"
          />
          <div v-else class="frame-missing">
            <i class="pi pi-image" aria-hidden="true" />
            <span>This camera's frame isn't available right now. Its assets are still being watched.</span>
          </div>
          <AssetZoneOverlay
            v-if="!frameFailed"
            :zones="zones"
            :highlighted="highlighted"
            interactive
            @hover="highlighted = $event"
            @select="focusRow"
          />
        </div>

        <ul class="asset-list" :aria-label="`Assets marked on ${camera}`">
          <li
            v-for="(asset, index) in assets"
            :id="`asset-row-${asset.id}`"
            :key="asset.id"
            class="asset-row"
            :class="{ highlighted: highlighted === asset.id, off: !asset.enabled }"
            :style="{ '--zone-color': zoneColor(index) }"
            @mouseenter="highlighted = asset.id"
            @mouseleave="highlighted = null"
          >
            <span class="asset-swatch" aria-hidden="true">
              <i :class="assetTypeInfo(asset.asset_type).icon" />
            </span>
            <div class="asset-main">
              <span class="asset-name">{{ asset.name }}</span>
              <span class="asset-meta">
                {{ assetTypeInfo(asset.asset_type).label }} · {{ zoneRegion(asset.zone) }}
              </span>
              <span class="asset-activity">
                <Tag
                  v-if="activityTags[asset.id]"
                  :value="activityTags[asset.id].label"
                  :severity="activityTags[asset.id].severity"
                  :title="activityTags[asset.id].title"
                />
                <span v-else class="muted-note">Quiet this week</span>
              </span>
            </div>
            <div class="asset-actions">
              <span class="watch-toggle">
                <ToggleSwitch
                  :model-value="asset.enabled"
                  :disabled="busy.has(asset.id)"
                  :aria-label="`Watch ${asset.name}`"
                  @update:model-value="emit('toggle', asset, $event)"
                />
                <!-- The switch's own name and checked state already say this
                     to a screen reader; the word is for the eye. -->
                <span class="watch-label" aria-hidden="true">{{ asset.enabled ? 'Watching' : 'Off' }}</span>
              </span>
              <Button
                icon="pi pi-pencil"
                text
                rounded
                severity="secondary"
                :aria-label="`Edit ${asset.name}`"
                @click="emit('edit', asset)"
              />
              <Button
                icon="pi pi-trash"
                text
                rounded
                severity="danger"
                :aria-label="`Remove ${asset.name}`"
                @click="emit('remove', asset)"
              />
            </div>
          </li>
        </ul>
      </div>

      <div v-if="assets.length" class="card-footer">
        <Button
          label="Mark another asset"
          icon="pi pi-plus"
          size="small"
          outlined
          :disabled="atLimit"
          @click="emit('add')"
        />
        <span v-if="atLimit" class="muted-note">
          {{ perCameraLimit }} is the most one camera can hold — remove one to add another.
        </span>
      </div>
    </template>
  </Card>
</template>

<style scoped>
.card-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.camera-name {
  margin: 0;
  font-size: 1.05rem;
  font-weight: 700;
  min-width: 0;
  overflow-wrap: anywhere;
}

.empty-camera {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
}

.camera-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(0, 1fr);
  gap: 1.1rem;
  align-items: start;
}

.camera-frame {
  position: relative;
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--card2);
  line-height: 0;
}

.camera-frame-image {
  display: block;
  width: 100%;
  height: auto;
}

.frame-missing {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 2rem 1rem;
  line-height: 1.4;
  text-align: center;
  color: var(--muted);
  font-size: 0.85rem;
}

.frame-missing .pi {
  font-size: 1.5rem;
}

.asset-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.asset-row {
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.55rem 0.6rem;
  border: 1px solid var(--border);
  border-left: 4px solid var(--zone-color);
  border-radius: var(--radius-sm);
  background: var(--card2);
  transition:
    border-color 0.15s var(--ease),
    background 0.15s var(--ease);
}

.asset-row.highlighted {
  background: var(--card-hover);
  border-color: var(--border-strong);
  border-left-color: var(--zone-color);
}

.asset-row.off .asset-name {
  color: var(--muted);
}

.asset-swatch {
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  width: 2rem;
  height: 2rem;
  border-radius: 50%;
  background: color-mix(in srgb, var(--zone-color) 22%, transparent);
  color: var(--text);
}

.asset-main {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  min-width: 0;
  flex: 1 1 auto;
}

.asset-name {
  font-weight: 600;
  overflow-wrap: anywhere;
}

.asset-meta {
  font-size: 0.78rem;
  color: var(--muted);
}

.asset-activity {
  margin-top: 0.15rem;
}

.asset-actions {
  display: flex;
  align-items: center;
  gap: 0.15rem;
  flex: 0 0 auto;
}

.watch-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  margin-right: 0.25rem;
}

.watch-label {
  font-size: 0.78rem;
  color: var(--text-dim);
  min-width: 3.9rem;
}

.card-footer {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-top: 1rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.82rem;
}

@media (max-width: 760px) {
  .camera-layout {
    grid-template-columns: minmax(0, 1fr);
  }
}

@media (max-width: 420px) {
  /* A phone's width can't fit the switch's word beside the buttons. */
  .watch-label {
    display: none;
  }
  .asset-row {
    align-items: flex-start;
  }
}
</style>
