<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Dialog from 'primevue/dialog'
import { getFailedAnalysisQueue } from '../../api/ai'
import type { AnalysisFailure } from '../../api/types'
import { fmtTs } from '../../api/constants'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

const emit = defineEmits<{ close: [] }>()

const loading = ref(true)
const failures = ref<AnalysisFailure[]>([])

async function load() {
  loading.value = true
  try {
    failures.value = await getFailedAnalysisQueue()
  } catch {
    failures.value = []
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <Dialog
    :visible="true"
    modal
    dismissable-mask
    header="Failed Analyses"
    :style="{ width: '38rem' }"
    :draggable="false"
    @update:visible="emit('close')"
  >
    <div v-if="loading" style="padding: 1rem"><LoadingIndicator /></div>
    <p v-else-if="!failures.length" style="color: var(--muted); font-size: 0.85rem">No failed analyses.</p>
    <div v-else class="failure-list">
      <div v-for="item in failures" :key="item.clip_id" class="failure-row">
        <div class="failure-row-header">
          <span class="failure-camera">{{ item.camera }}</span>
          <span class="failure-time">{{ fmtTs(item.completed_at) }}</span>
        </div>
        <p class="failure-message">{{ item.error_message || 'Unknown error' }}</p>
        <span v-if="item.retry_count" class="failure-retries">Retried {{ item.retry_count }}×</span>
      </div>
    </div>
  </Dialog>
</template>

<style scoped>
.failure-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  max-height: 60vh;
  overflow-y: auto;
}

.failure-row {
  padding: 0.6rem 0.7rem;
  background: var(--card-hover);
  border: 1px solid var(--border);
  border-radius: 6px;
}

.failure-row-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.failure-camera {
  font-size: 0.85rem;
  font-weight: 600;
}

.failure-time {
  font-size: 0.75rem;
  color: var(--muted);
}

.failure-message {
  margin: 0.35rem 0 0;
  font-size: 0.82rem;
  color: var(--danger);
  white-space: pre-wrap;
  word-break: break-word;
}

.failure-retries {
  display: inline-block;
  margin-top: 0.35rem;
  font-size: 0.72rem;
  color: var(--muted);
}
</style>
