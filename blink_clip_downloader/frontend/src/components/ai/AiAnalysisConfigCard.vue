<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import ToggleSwitch from 'primevue/toggleswitch'
import { getCameraConfigs, updateCameraConfigs } from '../../api/ai'
import type { CameraConfig } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

type EditableCameraConfig = CameraConfig & { auto_analyze: boolean }

const toast = useToastStore()
const visible = ref(false)
const loading = ref(true)
const saving = ref(false)
const loadError = ref(false)
const configs = ref<EditableCameraConfig[]>([])
const savedConfigs = ref<EditableCameraConfig[]>([])

const enabledCount = computed(() => configs.value.filter((config) => config.auto_analyze).length)
const allEnabled = computed({
  get: () => configs.value.length > 0 && enabledCount.value === configs.value.length,
  set: (enabled: boolean) => {
    for (const config of configs.value) config.auto_analyze = enabled
  },
})

function normalizeConfig(config: CameraConfig): EditableCameraConfig {
  return { ...config, auto_analyze: config.auto_analyze !== false }
}

function cameraInputId(camera: string): string {
  let slug = camera
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+/, '')
  while (slug.endsWith('-')) slug = slug.slice(0, -1)
  return `ai-analysis-${slug || 'camera'}`
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    configs.value = (await getCameraConfigs()).map(normalizeConfig)
    savedConfigs.value = configs.value.map((config) => ({ ...config }))
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

async function open() {
  if (!loadError.value) await load()
  visible.value = true
}

function restoreSavedConfigs() {
  configs.value = savedConfigs.value.map((config) => ({ ...config }))
}

function cancel() {
  restoreSavedConfigs()
  visible.value = false
}

async function save() {
  saving.value = true
  try {
    // PUT replaces the complete camera-config array. Re-read it immediately
    // before saving so edits made by the Camera Configurations or Vehicles
    // sections since this modal opened are not overwritten by a stale snapshot.
    const requestedByCamera = new Map(configs.value.map((config) => [config.camera, config]))
    const { configs: payload } = await updateCameraConfigs((latest) => {
      const latestCameras = new Set(latest.map((config) => config.camera))
      const merged = latest.map((config) => {
        const requested = requestedByCamera.get(config.camera)
        return requested ? { ...config, auto_analyze: requested.auto_analyze } : config
      })
      for (const requested of configs.value) {
        if (!latestCameras.has(requested.camera)) merged.push(requested)
      }
      return merged
    })
    configs.value = payload.map(normalizeConfig)
    savedConfigs.value = configs.value.map((config) => ({ ...config }))
    toast.show('AI analysis settings saved')
    visible.value = false
  } catch {
    toast.show('Failed to save AI analysis settings', true)
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <Card class="analysis-config-card">
    <template #title>AI Analysis Configuration</template>
    <template #subtitle>
      Choose which cameras automatically use AI analysis to reduce token usage. Manual Analyze Now remains available for
      every clip.
    </template>
    <template #content>
      <div class="config-summary">
        <span v-if="loading">Loading camera settings…</span>
        <span v-else-if="loadError" class="error-text">Unable to load camera settings.</span>
        <span v-else-if="!configs.length">No cameras found. Download at least one clip first.</span>
        <span v-else>{{ enabledCount }} of {{ configs.length }} cameras enabled for automatic analysis.</span>
        <Button label="Configure Cameras" icon="pi pi-sliders-h" size="small" :disabled="loading" @click="open" />
      </div>
    </template>
  </Card>

  <Dialog
    v-model:visible="visible"
    modal
    dismissable-mask
    header="AI Analysis Configuration"
    :style="{ width: '34rem', maxWidth: 'calc(100vw - 2rem)' }"
    :draggable="false"
    @hide="restoreSavedConfigs"
  >
    <p class="dialog-intro">
      Automatic analysis applies only to newly downloaded clips. Turn it off for cameras that do not need routine AI
      review to save tokens. You can still open any clip in the Library and click Analyze Now at any time.
    </p>

    <div v-if="loading" class="loading-state"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" :closable="false">
      Camera settings could not be loaded.
      <Button label="Retry" size="small" text @click="load" />
    </Message>
    <div v-else-if="!configs.length" class="empty-state">No cameras found. Download at least one clip first.</div>
    <template v-else>
      <div class="all-cameras-row">
        <div>
          <strong>Analyze all cameras automatically</strong>
          <span>Enable or disable automatic analysis everywhere.</span>
        </div>
        <ToggleSwitch v-model="allEnabled" input-id="ai-analysis-all" />
      </div>
      <div class="camera-list">
        <div v-for="config in configs" :key="config.camera" class="camera-row">
          <div>
            <label :for="cameraInputId(config.camera)">📷 {{ config.camera }}</label>
            <span>{{ config.auto_analyze ? 'Automatic analysis enabled' : 'Automatic analysis disabled' }}</span>
          </div>
          <ToggleSwitch v-model="config.auto_analyze" :input-id="cameraInputId(config.camera)" />
        </div>
      </div>
    </template>

    <template #footer>
      <Button label="Cancel" severity="secondary" text @click="cancel" />
      <Button
        label="Save Settings"
        :loading="saving"
        :disabled="saving || loading || loadError || !configs.length"
        @click="save"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.analysis-config-card {
  margin-bottom: 1.5rem;
}

.config-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
  color: var(--muted);
  font-size: 0.85rem;
}

.dialog-intro,
.all-cameras-row span,
.camera-row span {
  display: block;
  color: var(--muted);
  font-size: 0.82rem;
  line-height: 1.5;
}

.dialog-intro {
  margin: 0 0 1rem;
}

.all-cameras-row,
.camera-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.all-cameras-row {
  padding: 0.8rem 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}

.all-cameras-row strong,
.camera-row label {
  display: block;
  font-size: 0.86rem;
}

.camera-list {
  display: flex;
  flex-direction: column;
}

.camera-row {
  padding: 0.75rem 0;
  border-bottom: 1px solid var(--border);
}

.loading-state,
.empty-state {
  padding: 1rem 0;
  color: var(--muted);
  text-align: center;
}

.error-text {
  color: var(--danger);
}

@media (max-width: 480px) {
  .config-summary,
  .all-cameras-row,
  .camera-row {
    align-items: flex-start;
  }

  .config-summary {
    flex-direction: column;
  }
}
</style>
