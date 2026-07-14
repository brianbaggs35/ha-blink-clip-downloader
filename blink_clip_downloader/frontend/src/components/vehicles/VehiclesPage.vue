<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import Textarea from 'primevue/textarea'
import ToggleSwitch from 'primevue/toggleswitch'
import { getCameraConfigs, saveCameraConfigs } from '../../api/ai'
import { getVehicleSettings, saveVehicleSettings } from '../../api/vehicle'
import type { CameraConfig } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import VehicleZonePicker from './VehicleZonePicker.vue'

const toast = useToastStore()

const loading = ref(true)
const savingDescription = ref(false)
const savingCameras = ref(false)

const carDescription = ref('')
const configs = ref<CameraConfig[]>([])

async function load() {
  loading.value = true
  try {
    const [settings, cams] = await Promise.all([getVehicleSettings(), getCameraConfigs()])
    carDescription.value = settings.car_description
    configs.value = cams
  } catch {
    toast.show('Failed to load vehicle settings', true)
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function saveDescription() {
  savingDescription.value = true
  try {
    await saveVehicleSettings(carDescription.value.trim())
    toast.show('Protected vehicle description saved ✓')
  } catch {
    toast.show('Failed to save description', true)
  } finally {
    savingDescription.value = false
  }
}

async function saveCameras() {
  savingCameras.value = true
  try {
    await saveCameraConfigs(configs.value)
    toast.show('Vehicle camera settings saved ✓')
  } catch {
    toast.show('Failed to save camera settings', true)
  } finally {
    savingCameras.value = false
  }
}

function setZone(camera: CameraConfig, zone: CameraConfig['car_zone']) {
  camera.car_zone = zone
}

const carCameras = computed(() => configs.value.filter((c) => c.is_car_camera))
const protectionActive = computed(() => Boolean(carDescription.value.trim()))
const showInactiveWarning = computed(() => carCameras.value.length > 0 && !protectionActive.value)
</script>

<template>
  <div class="vehicles-page">
    <h2>Vehicles</h2>
    <p class="page-intro">
      Set up protected-vehicle monitoring: describe the vehicle you want watched, mark which camera(s) can see it, and
      precisely mark where it normally sits in each frame — sharpens proximity/contact detection and lets you pick out
      your specific car when several are parked close together (e.g. shared parking, apartment lots).
    </p>

    <Message v-if="showInactiveWarning" severity="warn" size="small" :closable="false" class="status-banner">
      A camera below is marked as seeing the protected vehicle, but no description is set yet — car-proximity rules
      won't activate until you save one below.
    </Message>
    <Message v-else-if="protectionActive" severity="success" size="small" :closable="false" class="status-banner">
      Protection active{{ carCameras.length ? ` for: ${carCameras.map((c) => c.camera).join(', ')}` : '' }}.
    </Message>

    <Card class="description-card">
      <template #title>Protected Vehicle Description</template>
      <template #subtitle>
        Describe the vehicle (make, model, color) so the AI can identify it in clips — e.g. "Silver Kia Forte sedan".
      </template>
      <template #content>
        <Textarea
          v-model="carDescription"
          rows="2"
          size="small"
          auto-resize
          class="description-input"
          placeholder="e.g. Silver Kia Forte sedan, usually parked nose-in"
        />
        <Button size="small" :disabled="savingDescription" :loading="savingDescription" @click="saveDescription">
          {{ savingDescription ? 'Saving…' : 'Save Description' }}
        </Button>
      </template>
    </Card>

    <div v-if="loading" class="muted-note">Loading…</div>
    <div v-else-if="!configs.length" class="muted-note">No cameras found. Download at least one clip first.</div>
    <div v-else class="camera-list">
      <Card v-for="cfg in configs" :key="cfg.camera" class="camera-card">
        <template #title>📷 {{ cfg.camera }}</template>
        <template #content>
          <div class="car-camera-row">
            <ToggleSwitch v-model="cfg.is_car_camera" :input-id="`vehicle-car-cam-${cfg.camera}`" />
            <label :for="`vehicle-car-cam-${cfg.camera}`" class="field-label"
              >Protected vehicle visible from this camera</label
            >
          </div>
          <VehicleZonePicker
            v-if="cfg.is_car_camera"
            :camera="cfg.camera"
            :model-value="cfg.car_zone"
            class="zone-picker"
            @update:model-value="setZone(cfg, $event)"
          />
        </template>
      </Card>
    </div>

    <div v-if="configs.length" class="save-row">
      <Button size="small" :disabled="savingCameras" :loading="savingCameras" @click="saveCameras">
        {{ savingCameras ? 'Saving…' : 'Save Camera Settings' }}
      </Button>
      <span class="muted-note">Changes apply immediately — no restart needed.</span>
    </div>
  </div>
</template>

<style scoped>
.vehicles-page {
  padding: 1.75rem;
  max-width: 900px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — on a narrow (mobile) viewport that pushed
     this whole page wider than the screen instead of wrapping its text,
     the same fix .auto-content (Automations/AI/Models) already has. */
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.status-banner {
  margin-bottom: 1rem;
}

.description-card {
  margin-bottom: 1.5rem;
}

.description-input {
  width: 100%;
  margin-bottom: 0.7rem;
}

.camera-list {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.car-camera-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.6rem;
}

.field-label {
  font-size: 0.82rem;
}

.zone-picker {
  margin-top: 0.5rem;
}

.save-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-top: 1.2rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
