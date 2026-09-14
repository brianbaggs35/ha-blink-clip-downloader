<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import Accordion from 'primevue/accordion'
import AccordionContent from 'primevue/accordioncontent'
import AccordionHeader from 'primevue/accordionheader'
import AccordionPanel from 'primevue/accordionpanel'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { getCameraConfigs, resolveCameraAlias, updateCameraConfigs } from '../../api/ai'
import type { CameraConfig } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import { useRefreshStore } from '../../stores/refresh'
import EmptyState from '../layout/EmptyState.vue'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// is_car_camera and car_zone are edited on the Vehicles tab now, not here —
// this component only owns description/custom_prompt, but still carries
// those two fields through unchanged on save (PUT /api/ai/camera-configs is
// a full-array replace, so this component, the AI analysis modal, and
// VehiclesPage must each round-trip the fields they don't own, or one would
// silently clobber the other's edits).
interface EditableConfig {
  camera: string
  description: string
  custom_prompt: string
  is_car_camera: boolean
  car_zone: CameraConfig['car_zone']
  auto_analyze: boolean
}

const toast = useToastStore()
const refresh = useRefreshStore()
const loading = ref(true)
const loadError = ref(false)
const configs = ref<EditableConfig[]>([])
const saving = ref(false)
let loadedSignature = ''

function toEditable(c: CameraConfig): EditableConfig {
  return {
    camera: c.camera,
    description: c.description,
    custom_prompt: c.custom_prompt,
    is_car_camera: c.is_car_camera,
    car_zone: c.car_zone,
    auto_analyze: c.auto_analyze !== false,
  }
}

// These are edit fields. A refresh landing while someone is part-way
// through typing a camera description would replace what they had written
// with whatever the server last stored — so a result is dropped if the form
// became dirty while it was in flight, alongside the usual newest-request
// check. The watcher below already declines to *start* a load over a dirty
// form; this covers a form that went dirty after one started.
let requestSeq = 0

async function load() {
  const seq = ++requestSeq
  const signatureAtStart = loadedSignature
  // Only for the first load, when there is nothing on screen yet. A
  // background refresh must not replace a form someone may be looking at
  // with a spinner — same "don't blank the page for a background refresh"
  // reasoning SyncModulePage's own silent reload documents.
  if (!configs.value.length) loading.value = true
  loadError.value = false
  try {
    const data = await getCameraConfigs()
    if (seq !== requestSeq) return
    if (isDirty() && loadedSignature === signatureAtStart) return
    configs.value = data.map(toEditable)
    loadedSignature = JSON.stringify(configs.value)
  } catch {
    if (seq === requestSeq) loadError.value = true
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

/** True when the form holds edits that have not been saved. */
function isDirty(): boolean {
  return configs.value.length > 0 && JSON.stringify(configs.value) !== loadedSignature
}

// Lets a glance at a collapsed accordion header show which cameras
// already have a description/prompt set, without opening every one.
function isConfigured(cfg: EditableConfig): boolean {
  return Boolean(cfg.description.trim() || cfg.custom_prompt.trim())
}
onMounted(load)
watch(
  () => refresh.tick,
  () => {
    if (isDirty()) return
    void load()
  },
)

async function save() {
  saving.value = true
  try {
    // PUT replaces the complete camera-config array. Re-read it immediately
    // before saving so this section cannot restore an old auto_analyze value
    // after the AI Analysis Configuration modal changes it.
    const localByCamera = new Map(configs.value.map((config) => [config.camera, config]))
    const { configs: payload } = await updateCameraConfigs((latest, aliases) => {
      const latestCameras = new Set(latest.map((config) => config.camera))
      const localOnly = configs.value.filter((config) => !latestCameras.has(config.camera))
      const latestOnly = latest.filter((config) => !localByCamera.has(config.camera))
      const renamedRemote = latestOnly[0]
      const renamedLocal =
        localOnly.length === 1 &&
        latestOnly.length === 1 &&
        renamedRemote &&
        resolveCameraAlias(localOnly[0].camera, aliases).toLowerCase() === renamedRemote.camera.toLowerCase()
          ? localOnly[0]
          : undefined
      const merged: CameraConfig[] = latest.map((config) => {
        const local = localByCamera.get(config.camera) || (renamedRemote === config ? renamedLocal : undefined)
        return local
          ? {
              ...config,
              description: local.description.trim(),
              custom_prompt: local.custom_prompt.trim(),
              auto_analyze: config.auto_analyze ?? local.auto_analyze,
            }
          : config
      })
      for (const local of configs.value) {
        if (!latestCameras.has(local.camera) && local !== renamedLocal) {
          merged.push({
            ...local,
            description: local.description.trim(),
            custom_prompt: local.custom_prompt.trim(),
          })
        }
      }
      return merged
    })
    configs.value = payload.map(toEditable)
    loadedSignature = JSON.stringify(configs.value)
    toast.show('Camera configs saved')
  } catch {
    toast.show('Failed to save camera configs', true)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div style="margin-bottom: 1.5rem">
    <h3 style="margin-bottom: 0.75rem; display: flex; align-items: center; gap: 0.5rem">
      📷 Camera Configurations
      <span style="font-size: 0.73rem; color: var(--muted); font-weight: 400">
        — Set per-camera purpose and custom prompts
      </span>
    </h3>
    <p style="font-size: 0.75rem; color: var(--muted); margin-bottom: 0.65rem">
      Marking a camera as seeing your protected vehicle, and setting where it sits in the frame, now lives on the
      <strong>Vehicles</strong> tab.
    </p>
    <div v-if="loading" style="padding: 1rem"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" :closable="false">Failed to load camera configs.</Message>
    <EmptyState v-else-if="!configs.length" title="No cameras found.">
      Download at least one clip to populate the camera list.
    </EmptyState>
    <Accordion v-else multiple>
      <AccordionPanel v-for="cfg in configs" :key="cfg.camera" :value="cfg.camera">
        <AccordionHeader>
          <span style="flex: 1; text-align: left">📷 {{ cfg.camera }}</span>
          <Tag v-if="isConfigured(cfg)" severity="secondary" value="Configured" style="margin-right: 0.5rem" />
        </AccordionHeader>
        <AccordionContent>
          <div style="margin-bottom: 0.45rem">
            <label
              :for="`cam-desc-${cfg.camera}`"
              style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.2rem"
              >Camera purpose / description</label
            >
            <InputText
              :id="`cam-desc-${cfg.camera}`"
              v-model="cfg.description"
              class="tag-input"
              fluid
              placeholder="e.g. Points at driveway, monitors the silver Kia Forte. Watch for anyone approaching the car."
            />
          </div>
          <div>
            <label
              :for="`cam-prompt-${cfg.camera}`"
              style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.2rem"
            >
              Custom AI prompt (overrides global prompt for this camera — optional)
            </label>
            <InputText
              :id="`cam-prompt-${cfg.camera}`"
              v-model="cfg.custom_prompt"
              class="tag-input"
              fluid
              placeholder="Leave empty to use the global AI prompt"
            />
          </div>
        </AccordionContent>
      </AccordionPanel>
    </Accordion>
    <div style="margin-top: 0.75rem; display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap">
      <Button size="small" :disabled="saving" @click="save">
        {{ saving ? '⏳ Saving…' : '💾 Save Camera Configs' }}
      </Button>
      <span style="font-size: 0.75rem; color: var(--muted)">Changes apply immediately — no restart needed</span>
    </div>
  </div>
</template>
