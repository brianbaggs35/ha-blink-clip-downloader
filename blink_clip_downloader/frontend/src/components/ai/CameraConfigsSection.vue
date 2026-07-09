<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { getCameraConfigs, saveCameraConfigs } from '../../api/ai'
import type { CameraConfig } from '../../api/types'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ carProtectionActive: boolean | null }>()

// Zone fields are typed loosely because Vue 3 auto-casts v-model on
// <input type="number"> to an actual Number once a value is entered, but
// leaves it as an empty string while the field is blank.
interface EditableConfig {
  camera: string
  description: string
  custom_prompt: string
  is_car_camera: boolean
  zone: { x_min: string | number; y_min: string | number; x_max: string | number; y_max: string | number }
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
    zone: {
      x_min: c.car_zone ? String(Math.round(c.car_zone.x_min * 100)) : '',
      y_min: c.car_zone ? String(Math.round(c.car_zone.y_min * 100)) : '',
      x_max: c.car_zone ? String(Math.round(c.car_zone.x_max * 100)) : '',
      y_max: c.car_zone ? String(Math.round(c.car_zone.y_max * 100)) : '',
    },
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

const anyCarCamera = computed(() => configs.value.some((c) => c.is_car_camera))
const showCarProtectionWarning = computed(() => anyCarCamera.value && props.carProtectionActive === false)

async function save() {
  saving.value = true
  try {
    const payload: CameraConfig[] = configs.value.map((c) => {
      const zoneVals = [c.zone.x_min, c.zone.y_min, c.zone.x_max, c.zone.y_max].map((v) => String(v).trim())
      const complete = zoneVals.every((v) => v !== '' && !isNaN(parseFloat(v)))
      return {
        camera: c.camera,
        description: c.description.trim(),
        custom_prompt: c.custom_prompt.trim(),
        is_car_camera: c.is_car_camera,
        car_zone: complete
          ? {
              x_min: parseFloat(zoneVals[0]) / 100,
              y_min: parseFloat(zoneVals[1]) / 100,
              x_max: parseFloat(zoneVals[2]) / 100,
              y_max: parseFloat(zoneVals[3]) / 100,
            }
          : null,
      }
    })
    await saveCameraConfigs(payload)
    toast.show('Camera configs saved ✓')
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
      <span style="font-size: 0.73rem; color: var(--muted); font-weight: 400"> — Set per-camera purpose and custom prompts </span>
    </h3>
    <div
      v-if="showCarProtectionWarning"
      style="background: rgba(245, 158, 11, 0.12); border: 1px solid var(--warn, #f59e0b); border-radius: 0.4rem; padding: 0.6rem 0.8rem; font-size: 0.78rem; margin-bottom: 0.65rem"
    >
      ⚠️ A camera below is marked "Protected vehicle visible from this camera", but no <strong>Protected Vehicle
      Description</strong> is set — car-proximity rules will not activate until you set one in the add-on's
      <strong>Configuration</strong> tab.
    </div>
    <div v-if="loading" style="color: var(--muted); font-size: 0.85rem; padding: 1rem">Loading…</div>
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
          <label style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.2rem">Camera purpose / description</label>
          <input
            v-model="cfg.description"
            type="text"
            class="tag-input"
            style="width: 100%"
            placeholder="e.g. Points at driveway, monitors the silver Kia Forte. Watch for anyone approaching the car."
          />
        </div>
        <div style="margin-bottom: 0.45rem">
          <label style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.2rem">
            Custom AI prompt (overrides global prompt for this camera — optional)
          </label>
          <input v-model="cfg.custom_prompt" type="text" class="tag-input" style="width: 100%" placeholder="Leave empty to use the global AI prompt" />
        </div>
        <div
          style="display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.55rem; background: var(--bg2, rgba(255, 255, 255, 0.04)); border-radius: 0.4rem; border: 1px solid var(--border, rgba(255, 255, 255, 0.1))"
        >
          <input :id="`cam-car-chk-${cfg.camera}`" v-model="cfg.is_car_camera" type="checkbox" style="cursor: pointer; width: 1rem; height: 1rem; accent-color: var(--accent, #5b9cf6)" />
          <label :for="`cam-car-chk-${cfg.camera}`" style="font-size: 0.76rem; color: var(--fg, #e2e8f0); cursor: pointer; line-height: 1.3">
            <strong>Protected vehicle visible from this camera</strong> — enables car-proximity alert rules
          </label>
        </div>
        <div
          v-if="cfg.is_car_camera"
          style="margin-top: 0.45rem; padding: 0.5rem 0.55rem; background: var(--bg2, rgba(255, 255, 255, 0.04)); border-radius: 0.4rem; border: 1px solid var(--border, rgba(255, 255, 255, 0.1))"
        >
          <label style="font-size: 0.76rem; color: var(--muted); display: block; margin-bottom: 0.35rem">
            Car zone (optional) — roughly where the vehicle normally sits, as % of the frame (0 = left/top edge, 100 =
            right/bottom edge). Sharpens accuracy when detection is ambiguous. Leave blank to skip.
          </label>
          <div style="display: grid; grid-template-columns: repeat(4, minmax(min(70px, 100%), 1fr)); gap: 0.4rem">
            <input v-model="cfg.zone.x_min" type="number" class="tag-input" placeholder="Left %" min="0" max="100" step="1" />
            <input v-model="cfg.zone.y_min" type="number" class="tag-input" placeholder="Top %" min="0" max="100" step="1" />
            <input v-model="cfg.zone.x_max" type="number" class="tag-input" placeholder="Right %" min="0" max="100" step="1" />
            <input v-model="cfg.zone.y_max" type="number" class="tag-input" placeholder="Bottom %" min="0" max="100" step="1" />
          </div>
        </div>
      </div>
    </div>
    <div style="margin-top: 0.75rem; display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap">
      <button class="btn sm" :disabled="saving" @click="save">{{ saving ? '⏳ Saving…' : '💾 Save Camera Configs' }}</button>
      <span style="font-size: 0.75rem; color: var(--muted)">Changes apply immediately — no restart needed</span>
    </div>
  </div>
</template>
