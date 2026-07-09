<script setup lang="ts">
import { computed } from 'vue'
import type { CameraStat } from '../../api/types'

const props = defineProps<{ cameras: CameraStat[] }>()
const current = defineModel<string>({ required: true })

const total = computed(() => props.cameras.reduce((sum, c) => sum + (c.total || 0), 0))
</script>

<template>
  <div id="camera-nav">
    <div class="cam-item" :class="{ active: current === 'all' }" data-camera="all" @click="current = 'all'">
      All Cameras<span class="cam-badge">{{ total }}</span>
    </div>
    <div
      v-for="cam in cameras"
      :key="cam.camera"
      class="cam-item"
      :class="{ active: current === cam.camera }"
      :data-camera="cam.camera"
      @click="current = cam.camera"
    >
      <span style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1">{{ cam.camera }}</span>
      <span class="cam-badge">{{ cam.total || 0 }}</span>
    </div>
  </div>
</template>
