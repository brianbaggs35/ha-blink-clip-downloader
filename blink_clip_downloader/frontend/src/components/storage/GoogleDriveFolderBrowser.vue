<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import Breadcrumb from 'primevue/breadcrumb'
import Button from 'primevue/button'
import Column from 'primevue/column'
import DataTable from 'primevue/datatable'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import { createGDriveFolder, listGDriveFolders } from '../../api/gdrive'
import type { GDriveFolder } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// Reused by GoogleDriveCard.vue (choosing/changing the default backup
// folder) and the Library "Upload to Drive" modal (choosing a one-off
// destination) — the one place this app ever lets a user browse their
// Google Drive. Deliberately folders-only: the query behind listGDriveFolders
// always filters mimeType to folders, so there is nothing here a user could
// interact with belonging to another app or file type.
const emit = defineEmits<{ select: [folder: { id: string; name: string }] }>()

const toast = useToastStore()

const path = ref<{ id: string; name: string }[]>([{ id: 'root', name: 'My Drive' }])
const folders = ref<GDriveFolder[]>([])
const loading = ref(true)
const loadError = ref(false)

const showNewFolderDialog = ref(false)
const newFolderName = ref('')
const creatingFolder = ref(false)

const currentParentId = computed(() => path.value[path.value.length - 1].id)
const currentFolderName = computed(() => path.value[path.value.length - 1].name)

const breadcrumbHome = computed(() => ({ label: path.value[0].name, command: () => navigateTo(0) }))
const breadcrumbItems = computed(() =>
  path.value.slice(1).map((entry, i) => ({ label: entry.name, command: () => navigateTo(i + 1) })),
)

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const res = await listGDriveFolders(currentParentId.value)
    folders.value = res.folders
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)

function navigateInto(folder: GDriveFolder) {
  path.value = [...path.value, { id: folder.id, name: folder.name }]
  void load()
}

function navigateTo(index: number) {
  path.value = path.value.slice(0, index + 1)
  void load()
}

function selectFolder(folder: { id: string; name: string }) {
  emit('select', folder)
}

function selectCurrentFolder() {
  emit('select', { id: currentParentId.value, name: currentFolderName.value })
}

async function createFolder() {
  const name = newFolderName.value.trim()
  if (!name) return
  creatingFolder.value = true
  try {
    await createGDriveFolder(name, currentParentId.value)
    toast.show(`Created folder "${name}"`)
    showNewFolderDialog.value = false
    newFolderName.value = ''
    void load()
  } catch {
    toast.show('Could not create folder', true)
  } finally {
    creatingFolder.value = false
  }
}
</script>

<template>
  <div class="gdrive-folder-browser">
    <div class="browser-toolbar">
      <Breadcrumb :home="breadcrumbHome" :model="breadcrumbItems" class="browser-breadcrumb" />
      <div class="browser-actions">
        <Button size="small" outlined label="New Folder" icon="pi pi-folder-plus" @click="showNewFolderDialog = true" />
        <Button size="small" label="Use This Folder" icon="pi pi-check" @click="selectCurrentFolder" />
      </div>
    </div>

    <div v-if="loading" class="browser-status"><LoadingIndicator /></div>
    <Message v-else-if="loadError" severity="error" :closable="false">Could not load Google Drive folders.</Message>
    <DataTable v-else :value="folders" data-key="id" size="small" class="browser-table">
      <template #empty>No subfolders here yet — create one, or use this folder.</template>
      <Column field="name" header="Name">
        <template #body="{ data }">
          <button type="button" class="folder-name-btn" @click="navigateInto(data)">
            <i class="pi pi-folder" /> {{ data.name }}
          </button>
        </template>
      </Column>
      <Column field="modified_time" header="Modified">
        <template #body="{ data }">{{
          data.modified_time ? new Date(data.modified_time).toLocaleDateString() : ''
        }}</template>
      </Column>
      <Column header="">
        <template #body="{ data }">
          <Button size="small" text label="Select" @click="selectFolder(data)" />
        </template>
      </Column>
    </DataTable>

    <Dialog
      v-model:visible="showNewFolderDialog"
      modal
      dismissable-mask
      header="New Folder"
      :style="{ width: '24rem' }"
      :draggable="false"
    >
      <InputText
        v-model="newFolderName"
        placeholder="Folder name"
        class="new-folder-input"
        autofocus
        @keyup.enter="createFolder"
      />
      <div class="dialog-actions">
        <Button size="small" severity="secondary" outlined label="Cancel" @click="showNewFolderDialog = false" />
        <Button
          size="small"
          :disabled="!newFolderName.trim() || creatingFolder"
          :loading="creatingFolder"
          label="Create"
          @click="createFolder"
        />
      </div>
    </Dialog>
  </div>
</template>

<style scoped>
.gdrive-folder-browser {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.browser-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.browser-breadcrumb {
  flex: 1;
  min-width: 0;
}

.browser-actions {
  display: flex;
  gap: 0.5rem;
  flex-shrink: 0;
}

.browser-status {
  padding: 1rem;
}

.browser-table {
  max-height: 320px;
  overflow-y: auto;
}

.folder-name-btn {
  background: none;
  border: none;
  padding: 0;
  color: inherit;
  font: inherit;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

.folder-name-btn:hover {
  text-decoration: underline;
}

.new-folder-input {
  width: 100%;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 0.5rem;
  margin-top: 1rem;
}
</style>
