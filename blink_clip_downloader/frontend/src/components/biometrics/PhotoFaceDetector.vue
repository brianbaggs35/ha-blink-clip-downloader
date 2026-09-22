<script setup lang="ts">
import { ref } from 'vue'
import FileUpload, { type FileUploadSelectEvent } from 'primevue/fileupload'
import Tag from 'primevue/tag'
import { describeApiError } from '../../api/client'
import { detectFacesInPhoto } from '../../api/faces'
import type { FaceCandidate } from '../../api/types'
import { photoDataUrl } from './photo'

const props = defineProps<{ available: boolean }>()
const emit = defineEmits<{ found: [faces: FaceCandidate[], label: string] }>()

interface PhotoResult {
  key: number
  name: string
  status: 'working' | 'done' | 'error'
  message: string
}
const results = ref<PhotoResult[]>([])
let nextKey = 0
const upload = ref<InstanceType<typeof FileUpload> | null>(null)

async function detect(file: File, row: PhotoResult) {
  try {
    const result = await detectFacesInPhoto(await photoDataUrl(file))
    if (result.error) {
      row.status = 'error'
      row.message = result.error
      return
    }
    row.status = result.faces.length ? 'done' : 'error'
    row.message = result.faces.length
      ? `${result.faces.length} face${result.faces.length === 1 ? '' : 's'} found`
      : 'No clear face found — try a closer, sharper photo'
    if (result.faces.length) emit('found', result.faces, file.name)
  } catch (e) {
    row.status = 'error'
    row.message = describeApiError(e, 'Detection failed — check your connection and try again')
  }
}

// One photo at a time, for the same reason clips are scanned one at a
// time: detection runs on the add-on's CPU, shared with clip analysis.
async function onSelect(event: FileUploadSelectEvent) {
  const files = (event.files ?? []) as File[]
  // The list below reports each photo; clearing the picker keeps it from
  // repeating the names, and lets the same photo be chosen again.
  upload.value?.clear()
  const rows = files.map((file) => {
    results.value.unshift({ key: nextKey++, name: file.name, status: 'working', message: 'Looking for faces…' })
    return results.value[0]
  })
  for (const [index, file] of files.entries()) await detect(file, rows[index])
}
</script>

<template>
  <div class="photo-face-detector">
    <p class="field-label">
      A clear, well-lit photo facing the camera works best. Every face in it is offered separately, so a group photo is
      fine — just pick the right face below.
    </p>
    <FileUpload
      ref="upload"
      mode="basic"
      size="small"
      :auto="false"
      custom-upload
      multiple
      accept="image/*"
      choose-label="Choose photos…"
      :disabled="!props.available"
      @select="onSelect"
    />
    <ul v-if="results.length" class="photo-results">
      <li v-for="row in results" :key="row.key">
        <span class="photo-name">{{ row.name }}</span>
        <Tag
          :severity="row.status === 'done' ? 'success' : row.status === 'error' ? 'warn' : 'secondary'"
          :value="row.message"
        />
      </li>
    </ul>
  </div>
</template>

<style scoped>
.photo-face-detector {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
  margin: 0;
}

.photo-results {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  font-size: 0.85rem;
}

.photo-results li {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.photo-name {
  overflow-wrap: anywhere;
}
</style>
