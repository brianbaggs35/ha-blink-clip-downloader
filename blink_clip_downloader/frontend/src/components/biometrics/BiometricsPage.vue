<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import Skeleton from 'primevue/skeleton'
import Tag from 'primevue/tag'
import { describeApiError } from '../../api/client'
import { deleteFacePhoto, listFaces, removePerson, updatePerson } from '../../api/faces'
import type { FacesResponse } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import FaceBypassActivityCard from './FaceBypassActivityCard.vue'
import FaceFinder from './FaceFinder.vue'
import { groupPeople, isResolutionMismatch, type Person } from './people'
import PersonCard from './PersonCard.vue'
import PersonPhotosDialog from './PersonPhotosDialog.vue'

const toast = useToastStore()
const confirm = useConfirm()
const refresh = useRefreshStore()

const loading = ref(true)
const loadFailed = ref(false)
const data = ref<FacesResponse | null>(null)
const finder = ref<InstanceType<typeof FaceFinder> | null>(null)

const available = computed(() => data.value?.available ?? true)
const frameWidth = computed(() => data.value?.frame_width ?? 640)
const people = computed<Person[]>(() => groupPeople(data.value?.faces ?? [], frameWidth.value))
const approvedCount = computed(() => people.value.filter((p) => p.approved).length)
const mismatchedCount = computed(
  () => data.value?.faces.filter((f) => isResolutionMismatch(f, frameWidth.value)).length ?? 0,
)
const legacyCount = computed(() => people.value.filter((p) => p.onlyLegacy).length)

// Whose photos the dialog shows. Held by name, so it follows a reload.
const managingName = ref<string | null>(null)
const managing = computed(() => people.value.find((p) => p.name === managingName.value) ?? null)
const busy = ref(false)

let loadSeq = 0
async function load() {
  const seq = ++loadSeq
  try {
    const result = await listFaces()
    if (seq !== loadSeq) return
    data.value = result
    loadFailed.value = false
  } catch {
    if (seq === loadSeq) loadFailed.value = true
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}
onMounted(load)
// A camera rename or a manual refresh; nothing on this page is an unsaved
// edit a reload could clobber — a rename in progress lives in its card.
watch(() => refresh.tick, load)

async function run(action: () => Promise<unknown>, success: string, failure: string) {
  busy.value = true
  try {
    await action()
    toast.show(success)
  } catch (e) {
    toast.show(`${failure}: ${describeApiError(e, 'check your connection and try again')}`, true)
  } finally {
    busy.value = false
    await load()
  }
}

function setApproved(person: Person, approved: boolean) {
  return run(
    () => updatePerson(person.name, { approved }),
    approved ? `${person.name} can now clear alerts` : `${person.name} no longer clears alerts`,
    'Failed to update approval',
  )
}

async function rename(person: Person, newName: string) {
  const trimmed = newName.trim()
  if (!trimmed) {
    toast.show('Name cannot be empty', true)
    return
  }
  if (trimmed === person.name) return
  // Someone else by that name, whatever its capitals — the same rule the
  // face finder enrolls by, so "brian" never becomes a second Brian.
  const folded = trimmed.toLowerCase()
  const other = people.value.find((p) => p !== person && p.name.toLowerCase() === folded)
  if (
    other &&
    !(await confirm(`"${other.name}" is already enrolled. Merge ${person.name}'s photos into theirs?`, 'Merge people?'))
  )
    return
  const target = other?.name ?? trimmed
  await run(
    () => updatePerson(person.name, { newName: target }),
    other ? `Merged into ${target}` : `Renamed to ${target}`,
    'Failed to rename',
  )
}

async function remove(person: Person) {
  const photos = `${person.photos.length} photo${person.photos.length === 1 ? '' : 's'}`
  const ok = await confirm(
    `Remove "${person.name}" (${photos}) from Biometrics? They will no longer be recognized. This cannot be undone.`,
    'Remove person?',
  )
  if (ok) await run(() => removePerson(person.name), `Removed ${person.name}`, 'Failed to remove')
}

async function removePhoto(id: number) {
  const person = managing.value
  if (!person) return
  const last = person.photos.length === 1
  const ok = await confirm(
    last
      ? `This is ${person.name}'s only photo — removing it removes ${person.name} entirely. Continue?`
      : `Remove this photo of ${person.name}? This cannot be undone.`,
    'Remove photo?',
  )
  if (ok)
    await run(() => deleteFacePhoto(id), last ? `Removed ${person.name}` : 'Photo removed', 'Failed to remove photo')
}

function addPhotos(person: Person) {
  managingName.value = null
  finder.value?.addPhotosFor(person.name)
}

function scanReportedClip(clipId: string) {
  void finder.value?.scanClip(clipId)
}
</script>

<template>
  <div class="biometrics-page">
    <div class="page-title-row">
      <h2>Biometrics</h2>
      <Tag severity="info" value="🧪 Advanced feature" />
    </div>
    <p class="page-intro">
      Enroll household members so clips of them can be recognized as routine. An approved, recognized person can
      automatically clear a clip's suspicious flag — but only when nobody unrecognized appears in the same clip.
      Entirely optional: with face recognition off, everything else works exactly the same.
    </p>

    <Message v-if="!available" severity="warn" size="small" :closable="false" class="status-banner">
      Face-recognition dependencies are not available on this system, so faces can't be found or enrolled.
    </Message>
    <Message
      v-else-if="data && !data.recognition_enabled"
      severity="warn"
      size="small"
      :closable="false"
      class="status-banner"
    >
      Face recognition is switched off, so nobody enrolled here is being recognized yet. Turn on
      <strong>Enable Local Face Recognition</strong> in the add-on's Configuration tab.
    </Message>
    <Message
      v-else-if="data && !data.analysis_enabled"
      severity="warn"
      size="small"
      :closable="false"
      class="status-banner"
    >
      Face recognition runs as part of AI clip analysis, which isn't configured — enrolled people won't be recognized
      until an AI provider is set up in the add-on's Configuration tab.
    </Message>
    <Message v-if="mismatchedCount" severity="info" size="small" :closable="false" class="status-banner">
      {{ mismatchedCount }} photo{{ mismatchedCount === 1 ? ' was' : 's were' }} captured at a different Face
      Recognition Resolution than the {{ frameWidth }}px recognition now uses, and will match less reliably — each
      person's Photos list shows which. Scanning a clip of them again adds photos that match.
    </Message>
    <Message v-if="legacyCount" severity="info" size="small" :closable="false" class="status-banner">
      {{ legacyCount === 1 ? '1 person was' : `${legacyCount} people were` }} enrolled with an earlier version, from
      smaller frames than recognition now uses. Use <strong>Add photos</strong> to add a few of each from a clip — they
      will be recognized far more reliably.
    </Message>

    <Message severity="secondary" size="small" :closable="false" class="privacy-banner">
      <strong>🔒 Everything here stays local.</strong> Photos, face data and names never leave this device and are never
      sent to any AI provider — not even a cloud one. Analysis only ever receives a nameless "recognized household
      member" signal; names are used afterwards, locally, to personalize your own notifications.
    </Message>

    <Card class="people-card">
      <template #title>Household members</template>
      <template #subtitle>
        The bypass is all-or-nothing per clip: it only clears a clip when every face in it belongs to someone
        <strong>approved</strong> below. A stranger — or someone recognized but not approved — standing next to an
        approved person still gets flagged.
      </template>
      <template #content>
        <div v-if="loading" class="people-grid">
          <Skeleton v-for="n in 2" :key="n" height="9rem" />
        </div>
        <Message v-else-if="loadFailed && !data" severity="error" size="small" :closable="false">
          Couldn't load enrolled people.
        </Message>
        <div v-else-if="!people.length" class="empty-state">
          <i class="pi pi-users" aria-hidden="true" />
          <p class="empty-title">Nobody enrolled yet</p>
          <p class="muted-note">
            Find someone's face in a clip, pick it and give it a name. A few photos from each camera they're seen on
            work best.
          </p>
          <Button
            label="Find faces"
            icon="pi pi-search"
            size="small"
            :disabled="!available"
            @click="finder?.reveal()"
          />
        </div>
        <template v-else>
          <div class="people-grid">
            <PersonCard
              v-for="person in people"
              :key="person.name"
              :person="person"
              @set-approved="setApproved(person, $event)"
              @rename="rename(person, $event)"
              @remove="remove(person)"
              @manage="managingName = person.name"
              @add-photos="addPhotos(person)"
            />
          </div>
          <p class="muted-note approved-summary">
            {{ approvedCount }} of {{ people.length }} enrolled
            {{ people.length === 1 ? 'person is' : 'people are' }} approved for the suspicious-flag bypass.
          </p>
        </template>
      </template>
    </Card>

    <FaceFinder ref="finder" :available="available" :people="people" @enrolled="load" />

    <FaceBypassActivityCard @scan-clip="scanReportedClip" />

    <PersonPhotosDialog
      :visible="managing !== null"
      :person="managing"
      :frame-width="frameWidth"
      :busy="busy"
      @update:visible="managingName = null"
      @remove-photo="removePhoto"
      @add-photos="managing && addPhotos(managing)"
    />
  </div>
</template>

<style scoped>
.biometrics-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  max-width: 1000px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — on a narrow (mobile) viewport that pushed
     this whole page wider than the screen instead of wrapping its text,
     the same fix .auto-content (Automations/AI/Models) already has. */
  min-width: 0;
  width: 100%;
}

.page-title-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.3rem;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.status-banner,
.privacy-banner {
  margin-bottom: 0.9rem;
}

.people-card {
  margin-bottom: 1.5rem;
}

.people-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr));
  gap: 1rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
  margin: 0;
}

.approved-summary {
  margin-top: 1rem;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  padding: 1.5rem 1rem;
  text-align: center;
}

.empty-state .pi {
  font-size: 1.75rem;
  color: var(--muted);
}

.empty-title {
  margin: 0;
  font-weight: 700;
}

.empty-state .muted-note {
  max-width: 420px;
}

@media (max-width: 600px) {
  .biometrics-page {
    padding: 1rem;
  }
  /* Cards inside cards: at full padding each, a person card's actions had
     under 300px of a phone's width and wrapped onto a third line. */
  .people-card :deep(.p-card-body) {
    padding: 1rem;
  }
}
</style>
