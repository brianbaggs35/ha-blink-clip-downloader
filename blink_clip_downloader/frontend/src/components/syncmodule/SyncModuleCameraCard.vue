<script setup lang="ts">
import ProgressSpinner from 'primevue/progressspinner'
import ToggleSwitch from 'primevue/toggleswitch'
import Tag from 'primevue/tag'
import { isBatteryLow as isLow } from '../../api/constants'
import type { SyncModuleCamera } from '../../api/types'
import AppIcon from '../icons/AppIcon.vue'

defineProps<{ camera: SyncModuleCamera; pending: boolean }>()
const emit = defineEmits<{ 'update:armed': [armed: boolean] }>()
</script>

<template>
  <div
    class="sm-cam-card"
    :class="{ 'sm-cam-armed': camera.armed, 'sm-cam-disarmed': !camera.armed, 'sm-cam-pending': pending }"
  >
    <div class="sm-cam-header">
      <span class="sm-cam-name">{{ camera.name }}</span>
      <Tag :severity="camera.online ? 'success' : 'secondary'" :value="camera.online ? 'Online' : 'Offline'" />
    </div>

    <div v-if="camera.battery_state" class="sm-cam-battery">
      <AppIcon :name="isLow(camera.battery_state) ? 'battery-low' : 'battery'" class="sm-cam-battery-icon" />
      <span>{{ isLow(camera.battery_state) ? 'Low battery' : 'Battery OK' }}</span>
    </div>

    <div class="sm-cam-footer">
      <span v-if="pending" class="sm-cam-pending-label">
        <ProgressSpinner class="sm-cam-spinner" :stroke-width="8" />
        Updating…
      </span>
      <Tag
        v-else
        class="sm-cam-armed-badge"
        :severity="camera.armed ? 'success' : 'danger'"
        :value="camera.armed ? 'Armed' : 'Disarmed'"
      />
      <ToggleSwitch
        :model-value="camera.armed"
        :disabled="pending"
        :aria-label="`${camera.armed ? 'Disarm' : 'Arm'} ${camera.name}`"
        @update:model-value="emit('update:armed', $event as boolean)"
      />
    </div>
  </div>
</template>

<style scoped>
.sm-cam-card {
  background: var(--card2, var(--card));
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 0.85rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  border-left: 3px solid var(--border);
  transition: border-left-color 0.15s ease;
}

.sm-cam-armed {
  border-left-color: var(--success);
}

.sm-cam-disarmed {
  border-left-color: var(--danger);
}

.sm-cam-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.sm-cam-name {
  font-weight: 600;
  font-size: 0.92rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.sm-cam-battery {
  display: flex;
  align-items: center;
  gap: 0.35rem;
  color: var(--muted);
  font-size: 0.82rem;
}

.sm-cam-battery-icon {
  width: 1rem;
  height: 1rem;
  flex-shrink: 0;
}

.sm-cam-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  margin-top: auto;
}

.sm-cam-pending {
  opacity: 0.75;
}

.sm-cam-pending-label {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.82rem;
  color: var(--muted);
}

.sm-cam-spinner {
  width: 1rem;
  height: 1rem;
}
</style>
