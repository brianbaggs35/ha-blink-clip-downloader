<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getCameraConfigs, saveCameraConfigs } from '../../api/ai'
import type { CameraConfig } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// is_car_camera and car_zone are edited on the Vehicles tab now, not here —
// this component only owns description/custom_prompt, but still carries
// those two fields through unchanged on save (PUT /api/ai/camera-configs is
// a full-array replace, so this component and VehiclesPage must each
// round-trip the fields they don't own, or one would silently clobber the
// other's edits).
interface EditableConfig {
  camera: string
  description: string
  custom_prompt: string
  is_car_camera: boolean
  car_zone: CameraConfig['car_zone']
}

const toast = useToastStore()
const loading = ref(true)
const loadError = ref(false)
const configs = ref<EditableConfig[]>([])
const saving = ref(false)

function toEditable(c: CameraConfig): EditableConfig {
  return {
    camera: c.camera,
    description: c.description,
    custom_prompt: c.custom_prompt,
    is_car_camera: c.is_car_camera,
    car_zone: c.car_zone,
  }
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const data = await getCameraConfigs()
    configs.value = data.map(toEditable)
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function save() {
  saving.value = true
  try {
    const payload: CameraConfig[] = configs.value.map((c) => ({
      camera: c.camera,
      description: c.description.trim(),
      custom_prompt: c.custom_prompt.trim(),
      is_car_camera: c.is_car_camera,
      car_zone: c.car_zone,
    }))
    await saveCameraConfigs(payload)
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
    <div v-else-if="loadError" style="color: var(--danger); font-size: 0.84rem">Failed to load camera configs.</div>
    <div v-else-if="!configs.length" style="color: var(--muted); font-size: 0.84rem; padding: 0.5rem 0">
      No cameras found. Download at least one clip to populate the camera list.
    </div>
    <div v-else style="display: flex; flex-direction: column; gap: 0.65rem">
      <div v-for="cfg in configs" :key="cfg.camera" class="status-card" style="padding: 0.85rem 1rem">
        <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.55rem">
          <span style="font-weight: 600; font-size: 0.88rem; color: var(--accent)">📷 {{ cfg.camera }}</span>
        </div>
        <div style="margin-bottom: 0.45rem">
          <label
            :for="`cam-desc-${cfg.camera}`"
            style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.2rem"
            >Camera purpose / description</label
          >
          <input
            :id="`cam-desc-${cfg.camera}`"
            v-model="cfg.description"
            type="text"
            class="tag-input"
            style="width: 100%"
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
          <input
            :id="`cam-prompt-${cfg.camera}`"
            v-model="cfg.custom_prompt"
            type="text"
            class="tag-input"
            style="width: 100%"
            placeholder="Leave empty to use the global AI prompt"
          />
        </div>
      </div>
    </div>
    <div style="margin-top: 0.75rem; display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap">
      <button class="btn sm" :disabled="saving" @click="save">
        {{ saving ? '⏳ Saving…' : '💾 Save Camera Configs' }}
      </button>
      <span style="font-size: 0.75rem; color: var(--muted)">Changes apply immediately — no restart needed</span>
    </div>
  </div>
</template>
