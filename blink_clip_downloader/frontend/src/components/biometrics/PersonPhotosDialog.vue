<script setup lang="ts">
import { computed } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import { faceThumbUrl } from '../../api/faces'
import { fmtTs } from '../../api/constants'
import type { FaceEnrollment } from '../../api/types'
import { initials, isLegacyPhoto, photoProblems, photoSource, type Person } from './people'

const props = defineProps<{ person: Person | null; frameWidth: number; busy: boolean }>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ 'remove-photo': [id: number]; 'add-photos': [] }>()

const photos = computed(() =>
  (props.person?.photos ?? []).map((photo) => ({
    photo,
    problems: photoProblems(photo, props.frameWidth),
    origin: originOf(photo),
  })),
)

function originOf(photo: FaceEnrollment): string | null {
  const source = photoSource(photo)
  if (!source) return null
  return source.kind === 'camera' ? `From ${source.camera}` : 'Uploaded photo'
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    :header="person ? `${person.name}'s photos` : 'Photos'"
    :style="{ width: 'min(760px, calc(100vw - 2rem))' }"
    class="person-photos-dialog"
  >
    <p class="field-label">
      Each photo is matched on its own, so one wrong or poor photo can cause a wrong or missed match. Remove any that
      aren't {{ person?.name ?? 'them' }}, or that are blurry.
    </p>
    <div class="photo-grid">
      <figure
        v-for="{ photo, problems, origin } in photos"
        :key="photo.id"
        class="photo-item"
        :class="{ flagged: problems.length }"
      >
        <img v-if="photo.has_thumbnail" :src="faceThumbUrl(photo.id)" :alt="`${photo.name}, photo ${photo.id}`" />
        <div v-else class="photo-placeholder" aria-hidden="true">{{ initials(photo.name) }}</div>
        <figcaption>
          <span v-if="origin" class="photo-origin">{{ origin }}</span>
          <span class="field-label">Added {{ fmtTs(photo.created_at) }}</span>
          <span v-if="isLegacyPhoto(photo)" class="field-label">Enrolled by an earlier version — no image kept.</span>
          <Message v-for="problem in problems" :key="problem" severity="warn" size="small" :closable="false">
            {{ problem }}
          </Message>
          <Button
            label="Remove photo"
            icon="pi pi-trash"
            size="small"
            text
            severity="danger"
            :disabled="busy"
            @click="emit('remove-photo', photo.id)"
          />
        </figcaption>
      </figure>
    </div>
    <template #footer>
      <Button label="Add photos" icon="pi pi-plus" size="small" @click="emit('add-photos')" />
      <Button label="Close" size="small" text severity="secondary" @click="visible = false" />
    </template>
  </Dialog>
</template>

<style scoped>
.field-label {
  font-size: 0.8rem;
  color: var(--muted);
  margin: 0 0 0.75rem;
}

.photo-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(130px, 100%), 1fr));
  gap: 0.75rem;
}

.photo-item {
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  padding: 0.5rem;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 10px);
  background: var(--card2);
}

.photo-item.flagged {
  border-color: var(--warn);
}

.photo-item img,
.photo-placeholder {
  width: 100%;
  aspect-ratio: 1;
  object-fit: cover;
  border-radius: calc(var(--radius-sm, 10px) - 4px);
}

.photo-placeholder {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.6rem;
  font-weight: 700;
  background: var(--tag-bg);
  color: var(--tag-text);
}

.photo-item figcaption {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.35rem;
}

.photo-item figcaption .field-label {
  margin: 0;
}

.photo-origin {
  font-size: 0.8rem;
  font-weight: 600;
  overflow-wrap: anywhere;
}
</style>
