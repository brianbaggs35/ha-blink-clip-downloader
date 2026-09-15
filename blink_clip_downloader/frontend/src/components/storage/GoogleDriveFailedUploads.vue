<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Paginator, { type PageState } from 'primevue/paginator'
import { clearFailedGDriveUploads, getFailedGDriveUploads, retryFailedGDriveUploads } from '../../api/gdrive'
import type { GDriveFailedUpload } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

// A failure that hits one clip usually hits every clip queued behind it,
// so this list is routinely hundreds long. It used to render all of them
// in one unbounded column; a page at a time is the whole point.
const PAGE_SIZE = 10

const emit = defineEmits<{ retried: [] }>()
const toast = useToastStore()
const refresh = useRefreshStore()
const confirm = useConfirm()

const failed = ref<GDriveFailedUpload[]>([])
const total = ref(0)
const first = ref(0)
const busyId = ref<string | null>(null)
const retryingAll = ref(false)
const clearingAll = ref(false)

/** The distinct reasons on this page, most common first. Hundreds of rows
 *  reading "Google Drive storage quota exceeded" say one thing, not two
 *  hundred, and the summary is what a person actually needs to read. */
const reasons = computed(() => {
  const counts = new Map<string, number>()
  for (const item of failed.value) {
    const reason = item.error_message || 'Unknown error'
    counts.set(reason, (counts.get(reason) ?? 0) + 1)
  }
  return [...counts.entries()].sort((a, b) => b[1] - a[1]).map(([reason, count]) => ({ reason, count }))
})

async function load() {
  try {
    const page = await getFailedGDriveUploads(PAGE_SIZE, first.value)
    failed.value = page.items ?? []
    total.value = page.total ?? 0
    // Clearing or retrying the last rows on the final page would otherwise
    // strand the paginator on a page that no longer exists, showing an
    // empty list with a non-zero position — same guard ArchivedClipsSection
    // makes after a delete.
    if (first.value > 0 && first.value >= total.value) {
      first.value = Math.max(0, first.value - PAGE_SIZE)
      await load()
    }
  } catch {
    failed.value = []
    total.value = 0
  }
}

function onPage(event: PageState) {
  first.value = event.first
  void load()
}

async function retryOne(clipId: string) {
  busyId.value = clipId
  try {
    await retryFailedGDriveUploads(clipId)
    toast.show('Retrying upload')
    await load()
    emit('retried')
  } catch {
    toast.show('Could not retry upload', true)
  } finally {
    busyId.value = null
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

async function dismissOne(clipId: string) {
  busyId.value = clipId
  try {
    await clearFailedGDriveUploads(clipId)
    await load()
    emit('retried')
  } catch {
    toast.show('Could not clear that failure', true)
  } finally {
    busyId.value = null
  }
}

async function clearAll() {
  // Worth confirming: this is the only destructive action in the panel,
  // and the count is the part a person needs to see before agreeing to it.
  if (!(await confirm(`Discard all ${total.value} failed upload(s)? The clips themselves are not touched.`))) {
    return
  }
  clearingAll.value = true
  try {
    const res = await clearFailedGDriveUploads()
    toast.show(`Cleared ${res.cleared} failed upload(s)`)
    first.value = 0
    await load()
    emit('retried')
  } catch {
    toast.show('Could not clear failed uploads', true)
  } finally {
    clearingAll.value = false
  }
}

onMounted(load)
// Read-only history list -- no draft/unsaved state to protect, so it's
// always safe to reload on every shared tick, including the one a camera
// rename triggers (see AppSidebar.vue). The exposed reload() stays for
// GoogleDriveCard's own retry-driven refresh, a separate trigger.
watch(() => refresh.tick, load)
defineExpose({ reload: load })
</script>

<template>
  <div v-if="total" class="gdrive-failed" data-testid="gdrive-failed">
    <div class="gdrive-failed-header">
      <span class="gdrive-failed-title">Failed Uploads ({{ total }})</span>
      <div class="gdrive-failed-actions">
        <Button
          size="small"
          severity="secondary"
          outlined
          :loading="retryingAll"
          :disabled="retryingAll || clearingAll"
          label="Retry All Failed"
          @click="retryAll"
        />
        <Button
          size="small"
          severity="danger"
          outlined
          :loading="clearingAll"
          :disabled="retryingAll || clearingAll"
          label="Clear All"
          @click="clearAll"
        />
      </div>
    </div>

    <!-- One line per distinct reason, so a Drive that filled up reads as
         one problem rather than as a page of separate ones. -->
    <p v-for="entry in reasons" :key="entry.reason" class="gdrive-failed-reason">
      {{ entry.reason }} — {{ entry.count }} on this page
    </p>

    <div v-for="item in failed" :key="item.clip_id" class="gdrive-failed-row">
      <div class="gdrive-failed-info">
        <span class="gdrive-failed-camera">{{ item.camera }}</span>
        <span class="gdrive-failed-error">{{ item.error_message || 'Unknown error' }}</span>
      </div>
      <div class="gdrive-failed-row-actions">
        <Button
          size="small"
          severity="secondary"
          text
          :loading="busyId === item.clip_id"
          :disabled="busyId === item.clip_id"
          label="Retry"
          @click="retryOne(item.clip_id)"
        />
        <Button
          size="small"
          severity="secondary"
          text
          :disabled="busyId === item.clip_id"
          :aria-label="`Dismiss the failed upload for ${item.camera}`"
          title="Dismiss this failure without retrying it"
          label="✕"
          @click="dismissOne(item.clip_id)"
        />
      </div>
    </div>

    <Paginator v-if="total > PAGE_SIZE" :first="first" :rows="PAGE_SIZE" :total-records="total" @page="onPage" />
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

.gdrive-failed-actions,
.gdrive-failed-row-actions {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}

.gdrive-failed-reason {
  margin: 0;
  font-size: 0.78rem;
  color: var(--text-dim);
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
