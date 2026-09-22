<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import Avatar from 'primevue/avatar'
import AvatarGroup from 'primevue/avatargroup'
import Button from 'primevue/button'
import Card from 'primevue/card'
import InputText from 'primevue/inputtext'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import { faceThumbUrl } from '../../api/faces'
import { initials, type Person } from './people'

const props = defineProps<{ person: Person }>()
const emit = defineEmits<{
  'set-approved': [approved: boolean]
  rename: [newName: string]
  remove: []
  manage: []
  'add-photos': []
}>()

const MAX_AVATARS = 4

const withPhotos = computed(() => props.person.photos.filter((p) => p.has_thumbnail))
const shown = computed(() => withPhotos.value.slice(0, MAX_AVATARS))
const photoCount = computed(() => props.person.photos.length)

const editing = ref(false)
const draft = ref('')
const renameForm = ref<HTMLFormElement | null>(null)

async function startEdit() {
  draft.value = props.person.name
  editing.value = true
  await nextTick()
  renameForm.value?.querySelector('input')?.focus()
}

function save() {
  emit('rename', draft.value)
  editing.value = false
}
</script>

<template>
  <Card class="person-card">
    <template #content>
      <div class="person-card-top">
        <AvatarGroup v-if="shown.length" class="person-avatars">
          <Avatar v-for="photo in shown" :key="photo.id" :image="faceThumbUrl(photo.id)" shape="circle" size="large" />
          <Avatar
            v-if="withPhotos.length > MAX_AVATARS"
            :label="`+${withPhotos.length - MAX_AVATARS}`"
            shape="circle"
            size="large"
          />
        </AvatarGroup>
        <Avatar v-else :label="initials(person.name)" shape="circle" size="large" class="person-initials" />

        <div class="person-heading">
          <form v-if="editing" ref="renameForm" class="person-rename" @submit.prevent="save">
            <InputText v-model="draft" size="small" class="rename-input" aria-label="New name" maxlength="60" />
            <Button type="submit" label="Save" text size="small" />
            <Button label="Cancel" text size="small" severity="secondary" @click="editing = false" />
          </form>
          <template v-else>
            <strong class="person-name">{{ person.name }}</strong>
            <Button
              icon="pi pi-pencil"
              text
              rounded
              size="small"
              severity="secondary"
              aria-label="Rename"
              title="Rename"
              @click="startEdit"
            />
          </template>
          <div class="person-meta">
            <Tag
              :severity="person.approved ? 'success' : person.mixedApproval ? 'warn' : 'secondary'"
              :value="person.mixedApproval ? 'Partially approved' : person.approved ? 'Approved' : 'Not approved'"
            />
            <span class="person-count">{{ photoCount }} photo{{ photoCount === 1 ? '' : 's' }}</span>
            <Tag
              v-if="person.problemCount"
              severity="warn"
              icon="pi pi-exclamation-triangle"
              :value="`${person.problemCount} to review`"
            />
          </div>
        </div>
      </div>

      <p v-if="person.onlyLegacy" class="person-hint">
        Enrolled with an earlier version, from smaller frames than recognition uses — adding a few photos from a clip
        will recognize {{ person.name }} much more reliably.
      </p>
      <p v-else-if="photoCount < 3" class="person-hint">
        More photos from different cameras and angles make recognition much more reliable.
      </p>

      <div class="person-card-actions">
        <div class="approved-row">
          <ToggleSwitch
            :model-value="person.approved"
            :input-id="`biometrics-approved-${person.name}`"
            @update:model-value="emit('set-approved', $event)"
          />
          <label :for="`biometrics-approved-${person.name}`" class="field-label">Approved for alert bypass</label>
        </div>
        <div class="person-buttons">
          <Button label="Photos" icon="pi pi-images" size="small" text @click="emit('manage')" />
          <Button label="Add photos" icon="pi pi-plus" size="small" text @click="emit('add-photos')" />
          <Button label="Remove" icon="pi pi-trash" size="small" text severity="danger" @click="emit('remove')" />
        </div>
      </div>
    </template>
  </Card>
</template>

<style scoped>
.person-card-top {
  display: flex;
  align-items: flex-start;
  gap: 0.9rem;
  margin-bottom: 0.6rem;
}

.person-avatars :deep(.p-avatar) {
  border: 2px solid var(--card);
}

.person-initials {
  background: var(--tag-bg);
  color: var(--tag-text);
  font-weight: 700;
}

.person-heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem;
  min-width: 0;
  flex: 1;
}

.person-name {
  font-size: 1rem;
  overflow-wrap: anywhere;
}

/* Wraps rather than pushing Save/Cancel out of a narrow card. */
.person-rename {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.25rem;
  width: 100%;
}

.rename-input {
  flex: 1;
  min-width: 0;
}

.person-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
}

.person-count,
.field-label,
.person-hint {
  font-size: 0.78rem;
  color: var(--muted);
}

.person-hint {
  margin: 0 0 0.6rem;
}

.person-card-actions {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.approved-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.person-buttons {
  display: flex;
  flex-wrap: wrap;
  gap: 0.1rem;
  margin-left: -0.5rem;
}
</style>
