<script setup lang="ts">
import { isBatteryLow as isLow } from '../../api/constants'
import type { BatteryStatus } from '../../api/types'
import AppIcon from '../icons/AppIcon.vue'

defineProps<{ readings: BatteryStatus[] }>()
const emit = defineEmits<{ 'select-camera': [camera: string] }>()

function voltageLabel(reading: BatteryStatus): string | null {
  if (reading.battery_voltage == null) return null
  return `${(reading.battery_voltage / 100).toFixed(2)}V`
}
</script>

<template>
  <div id="battery-strip" class="battery-strip">
    <button
      v-for="r in readings"
      :key="r.camera"
      type="button"
      class="battery-tile"
      :class="{ low: isLow(r.battery_state) }"
      @click="emit('select-camera', r.camera)"
    >
      <AppIcon :name="isLow(r.battery_state) ? 'battery-low' : 'battery'" class="battery-tile-icon" />
      <span class="battery-tile-info">
        <span class="battery-tile-camera">{{ r.camera }}</span>
        <span class="battery-tile-state">
          {{ isLow(r.battery_state) ? 'Low' : 'Normal' }}
          <template v-if="voltageLabel(r)"> · {{ voltageLabel(r) }}</template>
        </span>
      </span>
    </button>
  </div>
</template>
