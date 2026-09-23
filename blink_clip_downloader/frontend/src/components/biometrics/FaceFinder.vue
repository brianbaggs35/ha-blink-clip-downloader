<script setup lang="ts">
import { computed, nextTick, ref, watch } from 'vue'
import AutoComplete, { type AutoCompleteCompleteEvent } from 'primevue/autocomplete'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Message from 'primevue/message'
import Tab from 'primevue/tab'
import TabList from 'primevue/tablist'
import TabPanel from 'primevue/tabpanel'
import TabPanels from 'primevue/tabpanels'
import Tabs from 'primevue/tabs'
import ToggleSwitch from 'primevue/toggleswitch'
import { ApiError, describeApiError } from '../../api/client'
import { getClip } from '../../api/clips'
import { enrollFaces, groupFaces } from '../../api/faces'
import type { ClipListItem, FaceCandidate } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import ClipFaceScanner from './ClipFaceScanner.vue'
import FoundFaces from './FoundFaces.vue'
import type { FoundFace } from './found'
import type { Person } from './people'
import PhotoFaceDetector from './PhotoFaceDetector.vue'

const props = defineProps<{ available: boolean; people: Person[] }>()
const emit = defineEmits<{ enrolled: [] }>()

const toast = useToastStore()

// The server's limit (media_server/faces.py's _MAX_NAME_LENGTH), checked
// here too so a long paste is refused with a reason rather than a 400.
const MAX_NAME_LENGTH = 60

const source = ref<'clips' | 'photo'>('clips')
const scanner = ref<InstanceType<typeof ClipFaceScanner> | null>(null)
const root = ref<HTMLElement | null>(null)

const found = ref<FoundFace[]>([])
const groups = ref<string[][]>([])
const selected = ref<string[]>([])
const scanning = ref(false)

const name = ref('')
const approveNew = ref(true)
const enrolling = ref(false)
const nameSuggestions = ref<string[]>([])
// Set by a person's "Add photos": who the finder is collecting faces of, kept
// across enrolls so faces can be added from one camera, then another.
const addingFor = ref<string | null>(null)

const trimmedName = computed(() => name.value.trim())
// Case-insensitively, so "brian" adds to Brian rather than quietly starting
// a second person whose photos recognition would never pool with his.
const existingPerson = computed(() => {
  const typed = trimmedName.value
  const folded = typed.toLowerCase()
  return props.people.find((p) => p.name === typed) ?? props.people.find((p) => p.name.toLowerCase() === folded) ?? null
})
const targetName = computed(() => existingPerson.value?.name ?? trimmedName.value)
const selectedFaces = computed(() => found.value.filter((f) => selected.value.includes(f.id)))
const enrollLabel = computed(() => {
  const count = `${selected.value.length} photo${selected.value.length === 1 ? '' : 's'}`
  if (existingPerson.value) return `Add ${count} to ${existingPerson.value.name}`
  return trimmedName.value ? `Enroll as ${trimmedName.value}` : 'Enroll'
})

// A picked face that analysis already recognizes as someone *else* is the
// mistake that teaches recognition to confuse two people — and if the
// target is approved, lets one of them clear the other's alerts.
const conflictingMatches = computed(() => [
  ...new Set(
    selectedFaces.value
      .map((f) => f.match?.name)
      .filter((matched): matched is string => !!matched && matched !== targetName.value),
  ),
])
const spansGroups = computed(() => groups.value.filter((g) => g.some((id) => selected.value.includes(id))).length > 1)

function completeName(event: AutoCompleteCompleteEvent) {
  const query = event.query.trim().toLowerCase()
  nameSuggestions.value = props.people.map((p) => p.name).filter((n) => n.toLowerCase().includes(query))
}

// Grouping runs over everything found so far, so a person seen in three
// clips ends up as one group. Held server-side, a candidate can also expire
// (an hour, or a restart); those are dropped here rather than offered.
let groupSeq = 0
async function regroup() {
  const seq = ++groupSeq
  if (!found.value.length) {
    groups.value = []
    return
  }
  try {
    const result = await groupFaces(found.value.map((f) => f.id))
    if (seq !== groupSeq) return
    // Current (by the sequence check): anything found since would have sent
    // a newer request, so every face still held has an answer here.
    const expired = new Set(result.expired)
    found.value = found.value.filter((f) => !expired.has(f.id)).map((f) => ({ ...f, match: result.matches[f.id] }))
    selected.value = selected.value.filter((id) => !expired.has(id))
    groups.value = result.groups
  } catch {
    // Ungrouped faces still show, each on its own — see FoundFaces.
  }
}

// Enrolling, renaming or removing someone changes who each found face is
// recognized as: regroup, which answers again against the people as they
// are now. And a person removed or renamed mid-way is no longer being added to.
watch(
  () => props.people,
  (people) => {
    if (addingFor.value && !people.some((p) => p.name === addingFor.value)) stopAdding()
    void regroup()
  },
)
// Typing someone else's name means these faces are for them instead.
watch(trimmedName, (typed) => {
  if (addingFor.value && typed && typed.toLowerCase() !== addingFor.value.toLowerCase()) addingFor.value = null
})

function addFound(faces: FaceCandidate[], source: FoundFace['source']) {
  found.value = [...found.value, ...faces.map((face) => ({ ...face, source }))]
  void regroup()
}

function onClipFaces(faces: FaceCandidate[], clip: ClipListItem) {
  addFound(faces, { kind: 'clip', clipId: clip.id, camera: clip.camera, timestamp: clip.timestamp })
}

function onPhotoFaces(faces: FaceCandidate[], label: string) {
  addFound(faces, { kind: 'photo', label })
}

function clearFound() {
  found.value = []
  groups.value = []
  selected.value = []
}

function addTo(person: string, ids: string[]) {
  name.value = person
  selected.value = [...new Set([...selected.value, ...ids])]
}

async function enroll() {
  const target = targetName.value
  if (!target) {
    toast.show('Enter a name for the selected faces', true)
    return
  }
  if (target.length > MAX_NAME_LENGTH) {
    toast.show(`Names can be up to ${MAX_NAME_LENGTH} characters`, true)
    return
  }
  const ids = [...selected.value]
  enrolling.value = true
  try {
    const result = await enrollFaces(target, ids, approveNew.value)
    if ('error' in result) {
      toast.show(result.error, true)
      await regroup()
      return
    }
    const photos = `${result.enrolled} photo${result.enrolled === 1 ? '' : 's'}`
    const done = result.existing ? `Added ${photos} to ${result.name}` : `Enrolled ${photos} of ${result.name}`
    toast.show(result.expired ? `${done} — ${result.expired} had expired; scan again to add them` : done)
    const enrolled = new Set(ids)
    found.value = found.value.filter((f) => !enrolled.has(f.id))
    selected.value = []
    name.value = addingFor.value ?? ''
    approveNew.value = true
    // The page reloads its people, which regroups (see the watch above).
    emit('enrolled')
  } catch (e) {
    toast.show(`Enrollment failed: ${describeApiError(e, 'check your connection and try again')}`, true)
  } finally {
    enrolling.value = false
  }
}

/** Scroll the finder into view. */
function reveal() {
  root.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

/** Scan one clip, wherever it is — a "missed match" report's clip, say. */
async function scanClip(clipId: string) {
  source.value = 'clips'
  reveal()
  try {
    const clip = await getClip(clipId)
    await nextTick()
    scanner.value?.scan([{ ...clip, notified: false, face_recognized: false }])
  } catch (e) {
    toast.show(
      e instanceof ApiError && e.status === 404
        ? 'That clip is no longer in the library'
        : "Couldn't open that clip — try again",
      true,
    )
  }
}

/** Point the finder at adding photos of *person*, until told otherwise. */
function addPhotosFor(person: string) {
  addingFor.value = person
  name.value = person
  reveal()
}

// Whatever the name field holds is theirs: typing anyone else's name has
// already ended adding (see the watch above).
function stopAdding() {
  name.value = ''
  addingFor.value = null
}

defineExpose({ reveal, scanClip, addPhotosFor })
</script>

<template>
  <div ref="root" class="face-finder-anchor">
    <Card class="face-finder">
      <template #title>Add faces</template>
      <template #subtitle>
        Find faces in your clips (or upload a photo), pick the ones that show the person, and give them a name. Faces
        from the cameras themselves recognize best — they look the way that person will look next time.
      </template>
      <template #content>
        <Message v-if="addingFor" severity="info" :closable="false" class="adding-for">
          <div class="adding-for-body">
            <span>
              Adding photos of <strong>{{ addingFor }}</strong
              >. Scan clips from each camera they're seen on — or upload a photo — then pick their faces below.
            </span>
            <Button label="Done" size="small" text @click="stopAdding" />
          </div>
        </Message>
        <Tabs v-model:value="source">
          <TabList>
            <Tab value="clips">From clips</Tab>
            <Tab value="photo">From a photo</Tab>
          </TabList>
          <TabPanels>
            <TabPanel value="clips">
              <ClipFaceScanner
                ref="scanner"
                :available="props.available"
                @found="onClipFaces"
                @scanning-change="scanning = $event"
              />
            </TabPanel>
            <TabPanel value="photo">
              <PhotoFaceDetector :available="props.available" @found="onPhotoFaces" />
            </TabPanel>
          </TabPanels>
        </Tabs>

        <div v-if="found.length" class="found-section">
          <FoundFaces v-model:selected="selected" :faces="found" :groups="groups" @add-to="addTo" />
          <Button
            class="clear-found-btn"
            label="Clear found faces"
            size="small"
            text
            severity="secondary"
            :disabled="scanning"
            @click="clearFound"
          />
        </div>

        <section v-if="selected.length" class="enroll-bar" aria-label="Enroll selected faces">
          <div class="enroll-bar-fields">
            <label for="biometrics-name" class="field-label">
              {{ selected.length }} face{{ selected.length === 1 ? '' : 's' }} selected — who is this?
            </label>
            <AutoComplete
              v-model="name"
              input-id="biometrics-name"
              :suggestions="nameSuggestions"
              dropdown
              :pt="{ dropdown: { 'aria-label': 'Show enrolled people' } }"
              :show-empty-message="false"
              size="small"
              placeholder="Name, e.g. Brian"
              @complete="completeName"
            />
          </div>
          <div v-if="existingPerson" class="field-label enroll-bar-note">
            Adds to {{ existingPerson.name }}, who stays
            <strong>{{ existingPerson.approved ? 'approved' : 'not approved' }}</strong> for the alert bypass.
          </div>
          <div v-else class="enroll-bar-approve">
            <ToggleSwitch v-model="approveNew" input-id="biometrics-approve-new" />
            <label for="biometrics-approve-new" class="field-label"
              >Approve to clear alerts when only they appear</label
            >
          </div>
          <Message v-if="conflictingMatches.length" severity="warn" size="small" :closable="false">
            Some of these faces are already recognized as {{ conflictingMatches.join(', ') }}. Enrolling them as
            {{ targetName || 'someone else' }} could make recognition confuse the two.
          </Message>
          <Message v-else-if="spansGroups" severity="info" size="small" :closable="false">
            These faces come from more than one group — make sure they're all the same person.
          </Message>
          <div class="enroll-bar-actions">
            <Button
              class="enroll-submit-btn"
              size="small"
              icon="pi pi-user-plus"
              :label="enrollLabel"
              :loading="enrolling"
              :disabled="enrolling || !props.available"
              @click="enroll"
            />
            <Button label="Clear selection" size="small" text severity="secondary" @click="selected = []" />
          </div>
        </section>
      </template>
    </Card>
  </div>
</template>

<style scoped>
.face-finder-anchor {
  scroll-margin-top: 1rem;
  margin-bottom: 1.5rem;
}

@media (max-width: 600px) {
  .face-finder :deep(.p-card-body) {
    padding: 1rem;
  }
}

.adding-for {
  margin-bottom: 1rem;
}

/* Done stays beside the text, which wraps, rather than dropping below it. */
.adding-for-body {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.adding-for-body > span {
  flex: 1;
  min-width: 0;
}

.adding-for-body > :last-child {
  flex-shrink: 0;
}

.found-section {
  margin-top: 1.25rem;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.5rem;
}

.found-section > :first-child {
  align-self: stretch;
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
  margin: 0;
}

/* Stays in view while scrolling through a long list of faces. */
.enroll-bar {
  position: sticky;
  bottom: 0.75rem;
  z-index: 2;
  margin-top: 1rem;
  padding: 0.9rem 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  border: 1px solid var(--accent);
  border-radius: var(--radius, 14px);
  background: var(--card);
  box-shadow: var(--shadow);
}

.enroll-bar-fields {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  max-width: 360px;
}

.enroll-bar-approve {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.enroll-bar-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}
</style>
