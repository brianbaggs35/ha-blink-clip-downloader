<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import { fmtSize, fmtTs } from '../../api/constants'
import { deleteClip, listClips } from '../../api/clips'
import type { ClipListItem } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

const PAGE_SIZE = 20

const toast = useToastStore()
const confirm = useConfirm()

const clips = ref<ClipListItem[]>([])
const loading = ref(true)
const loadError = ref(false)
const offset = ref(0)
const hasMore = ref(false)
const deletingId = ref<string | null>(null)

// /api/clips has no total-count response (unlike the Suspicious Feed's
// dedicated endpoint), so pagination here is a simple prev/next rather than
// a full PrimeVue Paginator: fetch one extra row to detect a next page
// without needing that count.
async function load() {
  loading.value = true
  loadError.value = false
  try {
    const rows = await listClips({ archived: true, sort: 'newest', limit: PAGE_SIZE + 1, offset: offset.value })
    hasMore.value = rows.length > PAGE_SIZE
    clips.value = rows.slice(0, PAGE_SIZE)
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)
defineExpose({ reload: load })

function nextPage() {
  offset.value += PAGE_SIZE
  void load()
}

function prevPage() {
  offset.value = Math.max(0, offset.value - PAGE_SIZE)
  void load()
}

async function removeClip(clip: ClipListItem) {
  const question = clip.gdrive_backed_up
    ? 'Delete this clip? This also removes its Google Drive backup.'
    : 'Delete this clip?'
  if (!(await confirm(question))) return
  deletingId.value = clip.id
  try {
    await deleteClip(clip.id)
    toast.show('Clip deleted')
    void load()
  } catch {
    toast.show('Could not delete clip', true)
  } finally {
    deletingId.value = null
  }
}
</script>

<template>
  <div class="archived-clips-section">
    <Message severity="info" :closable="false" class="archived-note">
      Archived clips are stored in compressed monthly ZIP files and can't be played directly from here — delete them
      from this list, or back them up to Google Drive below.
    </Message>

    <div v-if="loading" class="archived-status"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" :closable="false">Failed to load archived clips.</Message>
    <template v-else>
      <DataTable :value="clips" data-key="id" size="small">
        <template #empty>No archived clips yet.</template>
        <Column field="camera" header="Camera" />
        <Column field="timestamp" header="Date">
          <template #body="{ data }">{{ fmtTs(data.timestamp) }}</template>
        </Column>
        <Column field="size_bytes" header="Size">
          <template #body="{ data }">{{ fmtSize(data.size_bytes) }}</template>
        </Column>
        <Column header="Drive Backup">
          <template #body="{ data }">
            <Tag v-if="data.gdrive_backed_up" severity="success" value="Backed up" />
            <Tag v-else severity="secondary" value="Not backed up" />
          </template>
        </Column>
        <Column header="">
          <template #body="{ data }">
            <Button
              size="small"
              severity="danger"
              text
              label="Delete"
              :disabled="deletingId === data.id"
              @click="removeClip(data)"
            />
          </template>
        </Column>
      </DataTable>

      <div class="archived-pager">
        <Button
          size="small"
          severity="secondary"
          outlined
          label="Previous"
          :disabled="offset === 0"
          @click="prevPage"
        />
        <Button size="small" severity="secondary" outlined label="Next" :disabled="!hasMore" @click="nextPage" />
      </div>
    </template>
  </div>
</template>

<style scoped>
.archived-clips-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.archived-note {
  margin: 0;
}

.archived-status {
  padding: 1rem;
}

.archived-pager {
  display: flex;
  justify-content: center;
  gap: 0.5rem;
}
</style>
