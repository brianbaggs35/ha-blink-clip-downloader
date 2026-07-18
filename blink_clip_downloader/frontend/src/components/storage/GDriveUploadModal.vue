<script setup lang="ts">
import { ref } from 'vue'
import Dialog from 'primevue/dialog'
import { uploadClipsToGDrive } from '../../api/gdrive'
import { useToastStore } from '../../stores/toast'
import GoogleDriveFolderBrowser from './GoogleDriveFolderBrowser.vue'

const props = defineProps<{ clipIds: string[] }>()
const emit = defineEmits<{ close: []; uploaded: [count: number] }>()

const toast = useToastStore()
const uploading = ref(false)

async function onFolderSelected(folder: { id: string; name: string }) {
  uploading.value = true
  try {
    const res = await uploadClipsToGDrive(props.clipIds, folder.id)
    toast.show(`Queued ${res.enqueued} clip(s) for upload to "${folder.name}"`)
    emit('uploaded', res.enqueued)
  } catch {
    toast.show('Could not queue clips for upload', true)
  } finally {
    uploading.value = false
  }
}
</script>

<template>
  <Dialog
    :visible="true"
    modal
    dismissable-mask
    :header="`Upload ${clipIds.length} clip(s) to Google Drive`"
    :style="{ width: '38rem' }"
    :draggable="false"
    @update:visible="emit('close')"
  >
    <p class="modal-intro">Choose a folder to upload the selected clip(s) into, or create a new one.</p>
    <GoogleDriveFolderBrowser :class="{ 'is-uploading': uploading }" @select="onFolderSelected" />
  </Dialog>
</template>

<style scoped>
.modal-intro {
  color: var(--muted);
  font-size: 0.85rem;
  margin-top: 0;
}

.is-uploading {
  opacity: 0.6;
  pointer-events: none;
}
</style>
