<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import { getFailedGDriveUploads, retryFailedGDriveUploads } from '../../api/gdrive'
import type { GDriveFailedUpload } from '../../api/types'
import { useToastStore } from '../../stores/toast'

const emit = defineEmits<{ retried: [] }>()
const toast = useToastStore()

const failed = ref<GDriveFailedUpload[]>([])
const retryingId = ref<string | null>(null)
const retryingAll = ref(false)

async function load() {
  try {
    failed.value = await getFailedGDriveUploads()
  } catch {
    failed.value = []
  }
}

async function retryOne(clipId: string) {
  retryingId.value = clipId
  try {
    await retryFailedGDriveUploads(clipId)
    toast.show('Retrying upload')
    await load()
    emit('retried')
  } catch {
    toast.show('Could not retry upload', true)
  } finally {
    retryingId.value = null
  }
}

async function retryAll() {
  retryingAll.value = true
  try {
    const res = await retryFailedGDriveUploads()
    toast.show(`Retrying ${res.retried} upload(s)`)
    await load()
    emit('retried')
  } catch {
    toast.show('Could not retry uploads', true)
  } finally {
    retryingAll.value = false
  }
}

onMounted(load)
defineExpose({ reload: load })
</script>

<template>
  <div v-if="failed.length" class="gdrive-failed">
    <div class="gdrive-failed-header">
      <span class="gdrive-failed-title">Failed Uploads ({{ failed.length }})</span>
      <Button
        size="small"
        severity="secondary"
        outlined
        :loading="retryingAll"
        :disabled="retryingAll"
        label="Retry All Failed"
        @click="retryAll"
      />
    </div>
    <div v-for="item in failed" :key="item.clip_id" class="gdrive-failed-row">
      <div class="gdrive-failed-info">
        <span class="gdrive-failed-camera">{{ item.camera }}</span>
        <span class="gdrive-failed-error">{{ item.error_message || 'Unknown error' }}</span>
      </div>
      <Button
        size="small"
        severity="secondary"
        text
        :loading="retryingId === item.clip_id"
        :disabled="retryingId === item.clip_id"
        label="Retry"
        @click="retryOne(item.clip_id)"
      />
    </div>
  </div>
</template>

<style scoped>
.gdrive-failed {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  margin: 0.75rem 0;
  padding: 0.75rem;
  background: var(--card-hover);
  border: 1px solid var(--border);
  border-radius: 6px;
}

.gdrive-failed-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.gdrive-failed-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--danger);
}

.gdrive-failed-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
  padding-top: 0.4rem;
  border-top: 1px solid var(--border);
}

.gdrive-failed-info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.gdrive-failed-camera {
  font-size: 0.85rem;
  font-weight: 600;
}

.gdrive-failed-error {
  font-size: 0.78rem;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
