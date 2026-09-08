<script setup lang="ts">
import Card from 'primevue/card'
import ProgressSpinner from 'primevue/progressspinner'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import type { SyncModuleInfo } from '../../api/types'
import SyncModuleCameraCard from './SyncModuleCameraCard.vue'

defineProps<{
  module: SyncModuleInfo
  pending: boolean
  pendingCameras: Set<string>
}>()
const emit = defineEmits<{
  'toggle-module': [armed: boolean]
  'toggle-camera': [camera: string, armed: boolean]
}>()
</script>

<template>
  <Card
    class="sync-module-card"
    :class="{
      'sync-module-armed': module.armed,
      'sync-module-disarmed': !module.armed,
      'sync-module-pending': pending,
    }"
  >
    <template #content>
      <div class="sm-module-header">
        <div class="sm-module-title">
          <strong>{{ module.name }}</strong>
          <Tag :severity="module.online ? 'success' : 'secondary'" :value="module.online ? 'Online' : 'Offline'" />
        </div>
        <div class="sm-module-arm">
          <span v-if="pending" class="sm-module-pending-label">
            <ProgressSpinner class="sm-module-spinner" stroke-width="8" />
            Updating…
          </span>
          <Tag v-else :severity="module.armed ? 'success' : 'danger'" :value="module.armed ? 'Armed' : 'Disarmed'" />
          <ToggleSwitch
            :model-value="!!module.armed"
            :disabled="pending"
            :aria-label="`${module.armed ? 'Disarm' : 'Arm'} ${module.name}`"
            @update:model-value="emit('toggle-module', $event as boolean)"
          />
        </div>
      </div>

      <div class="sm-module-info">
        <span v-if="module.version" class="sm-module-info-item">Firmware {{ module.version }}</span>
        <span v-if="module.serial" class="sm-module-info-item">Serial {{ module.serial }}</span>
        <span v-if="module.local_storage" class="sm-module-info-item">Local Storage active</span>
      </div>

      <p v-if="!module.cameras.length" class="muted-note">No cameras on this sync module.</p>
      <div v-else class="sm-camera-grid">
        <SyncModuleCameraCard
          v-for="cam in module.cameras"
          :key="cam.name"
          :camera="cam"
          :pending="pendingCameras.has(cam.name)"
          @update:armed="(armed) => emit('toggle-camera', cam.name, armed)"
        />
      </div>
    </template>
  </Card>
</template>

<style scoped>
.sync-module-card {
  border-top: 3px solid var(--border);
}

.sync-module-armed {
  border-top-color: var(--success);
}

.sync-module-disarmed {
  border-top-color: var(--danger);
}

.sync-module-pending {
  opacity: 0.85;
}

.sm-module-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.75rem;
  margin-bottom: 0.5rem;
}

.sm-module-title {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  font-size: 1.05rem;
}

.sm-module-arm {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.sm-module-pending-label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.85rem;
  color: var(--muted);
}

.sm-module-spinner {
  width: 1.1rem;
  height: 1.1rem;
}

.sm-module-info {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem 1rem;
  color: var(--muted);
  font-size: 0.82rem;
  margin-bottom: 1rem;
}

.sm-camera-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(220px, 100%), 1fr));
  gap: 0.85rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
