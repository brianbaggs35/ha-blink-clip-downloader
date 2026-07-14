<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import FileUpload, { type FileUploadSelectEvent } from 'primevue/fileupload'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import ToggleSwitch from 'primevue/toggleswitch'
import { deleteFacesByName, enrollFace, listFaces, renameFacesByName, setFacesApprovedByName } from '../../api/ai'
import type { FaceEnrollment } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'
import EnrollFromClipPicker from './EnrollFromClipPicker.vue'

interface PersonGroup {
  name: string
  ids: number[]
  photoCount: number
  approved: boolean
  mixedApproval: boolean
  createdAt: string
}

const toast = useToastStore()
const confirm = useConfirm()

const available = ref(true)
const loading = ref(true)
const faces = ref<FaceEnrollment[]>([])

type EnrollMode = 'clip' | 'photo'
const enrollMode = ref<EnrollMode>('clip')

const name = ref('')
const approvedOnEnroll = ref(true)
const enrolling = ref(false)

// "From a clip" mode (ADVANCED FEATURE, recommended) — pull several frames
// from a real clip and pick out whichever ones show the face clearly,
// across as many angles/lighting conditions as needed. This is what
// actually makes recognition robust enough to cut down false positives on
// a camera like a front door, where the very first frame of a
// motion-triggered clip often doesn't have a good angle on the face.
const selectedClipFrames = ref<string[]>([])

// "From a photo" mode — the simple single-photo flow from v5's first cut,
// kept for enrolling from a phone photo with no existing clip.
const selectedFile = ref<File | null>(null)
const previewUrl = ref('')

const editingName = ref<string | null>(null)
const editingNameValue = ref('')

async function load() {
  loading.value = true
  try {
    const data = await listFaces()
    available.value = data.available
    faces.value = data.faces
  } catch {
    /* non-fatal — mirrors the pre-Vue UI's console.error-only handling */
  } finally {
    loading.value = false
  }
}
onMounted(load)

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = reject
    reader.readAsDataURL(file)
  })
}

function onFileSelect(event: FileUploadSelectEvent) {
  const files = (event.files || []) as File[]
  const file = files[files.length - 1] || null
  selectedFile.value = file
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = file ? URL.createObjectURL(file) : ''
}

function clearSelection() {
  selectedFile.value = null
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = ''
}

// The Biometrics tab is v-if-gated (see App.vue) — fully destroyed on tab
// switch — so a preview object URL left over from an abandoned photo
// selection (switched tabs without enrolling or clicking Clear) must be
// revoked here too, or it leaks for the rest of the page's lifetime.
onUnmounted(() => {
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})

function resetEnrollForm() {
  name.value = ''
  approvedOnEnroll.value = true
  selectedClipFrames.value = []
  clearSelection()
}

async function enrollFromPhoto() {
  const trimmedName = name.value.trim()
  if (!trimmedName) {
    toast.show('Enter a name', true)
    return
  }
  if (!selectedFile.value) {
    toast.show('Choose a photo', true)
    return
  }
  enrolling.value = true
  try {
    const imageBase64 = await fileToBase64(selectedFile.value)
    await enrollFace(trimmedName, imageBase64, approvedOnEnroll.value)
    toast.show(`Enrolled ${trimmedName}`)
    resetEnrollForm()
    await load()
  } catch (e) {
    toast.show(`Enrollment failed: ${e instanceof Error ? e.message : 'unknown error'}`, true)
  } finally {
    enrolling.value = false
  }
}

async function enrollFromClipFrames() {
  const trimmedName = name.value.trim()
  if (!trimmedName) {
    toast.show('Enter a name', true)
    return
  }
  if (!selectedClipFrames.value.length) {
    toast.show('Select at least one frame that shows the face clearly', true)
    return
  }
  enrolling.value = true
  let succeeded = 0
  let failed = 0
  for (const frame of selectedClipFrames.value) {
    try {
      await enrollFace(trimmedName, frame, approvedOnEnroll.value)
      succeeded++
    } catch {
      failed++
    }
  }
  enrolling.value = false
  if (succeeded) {
    toast.show(
      failed
        ? `Enrolled ${succeeded} of ${succeeded + failed} photo(s) — ${failed} skipped (no face detected)`
        : `Enrolled ${succeeded} photo(s) for ${trimmedName}`,
    )
    resetEnrollForm()
    await load()
  } else {
    toast.show('Enrollment failed for every selected frame — no clear face detected', true)
  }
}

function enroll() {
  return enrollMode.value === 'clip' ? enrollFromClipFrames() : enrollFromPhoto()
}

async function toggleApproved(group: PersonGroup, approved: boolean) {
  try {
    await setFacesApprovedByName(group.name, approved)
    for (const f of faces.value) if (f.name === group.name) f.approved = approved
    toast.show(approved ? `${group.name} approved for auto-clear` : `${group.name} no longer approved`)
  } catch {
    toast.show('Failed to update approval', true)
  }
}

function startEdit(group: PersonGroup) {
  editingName.value = group.name
  editingNameValue.value = group.name
}

async function saveEdit(group: PersonGroup) {
  const trimmed = editingNameValue.value.trim()
  if (!trimmed) {
    toast.show('Name cannot be empty', true)
    return
  }
  try {
    await renameFacesByName(group.name, trimmed)
    for (const f of faces.value) if (f.name === group.name) f.name = trimmed
    editingName.value = null
  } catch {
    toast.show('Failed to rename', true)
  }
}

function cancelEdit() {
  editingName.value = null
}

async function remove(group: PersonGroup) {
  const photoWord = group.photoCount === 1 ? 'photo' : 'photos'
  const ok = await confirm(
    `Remove "${group.name}" (${group.photoCount} ${photoWord}) from Biometrics? This cannot be undone.`,
    'Remove enrollment?',
  )
  if (!ok) return
  try {
    await deleteFacesByName(group.name)
    await load()
    toast.show(`Removed ${group.name}`)
  } catch {
    toast.show('Failed to remove enrollment', true)
  }
}

const groupedPeople = computed<PersonGroup[]>(() => {
  const byName = new Map<string, FaceEnrollment[]>()
  for (const f of faces.value) {
    if (!byName.has(f.name)) byName.set(f.name, [])
    byName.get(f.name)!.push(f)
  }
  return [...byName.entries()]
    .map(([name, rows]) => ({
      name,
      ids: rows.map((r) => r.id),
      photoCount: rows.length,
      approved: rows.every((r) => r.approved),
      mixedApproval: rows.some((r) => r.approved) && rows.some((r) => !r.approved),
      createdAt: rows.map((r) => r.created_at).sort()[0] ?? '',
    }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

const approvedCount = computed(() => groupedPeople.value.filter((g) => g.approved).length)
</script>

<template>
  <div class="biometrics-page">
    <div class="page-title-row">
      <h2>Biometrics</h2>
      <Tag severity="info" value="🧪 Advanced feature" />
    </div>
    <p class="page-intro">
      Enroll household members so their clips can be recognized and treated as routine. An approved, recognized person
      can automatically clear a clip's suspicious flag — but only when no one else unrecognized or not-approved also
      appears in the same clip. This is entirely optional: with face recognition off (the default, set via
      <code>ai_face_recognition_enabled</code> in the add-on's Configuration tab), everything works exactly as it does
      without it — nothing here is required.
    </p>

    <Message severity="info" :closable="false" class="privacy-banner">
      <strong>🔒 Everything here stays local.</strong> Photos, face embeddings, and names never leave this
      device/network and are never sent to any AI provider. Even when using a cloud AI provider (Anthropic, OpenAI,
      Moondream Cloud, Ollama Cloud), analysis only ever receives a generic "recognized household member" signal — never
      a name, photo, or embedding. A recognized person's name is only ever used afterward, entirely locally, to
      personalize your own notifications (e.g. "Brian walked up the driveway").
    </Message>

    <Message severity="warn" :closable="false" class="safety-banner">
      <strong>Safety guarantee:</strong> the suspicious-flag bypass is all-or-nothing per clip. It only fires when
      <em>every</em> face detected belongs to an <strong>approved</strong> enrollment below — a single stranger, or a
      recognized-but-not-approved person, standing next to an approved family member still gets flagged normally.
    </Message>

    <Message v-if="!available" severity="warn" :closable="false">
      Face-recognition dependencies are not installed in this image — enrollment is disabled.
    </Message>

    <Card class="enroll-card">
      <template #title>Add a person</template>
      <template #subtitle>
        Recognition accuracy depends on enrolling a good range of angles/lighting — a single posed photo often doesn't
        match well against a camera catching someone mid-stride. Enrolling several frames from a real clip (recommended)
        fixes this directly.
      </template>
      <template #content>
        <div class="mode-toggle">
          <Button :outlined="enrollMode !== 'clip'" size="small" @click="enrollMode = 'clip'">
            🧪 From a clip (recommended)
          </Button>
          <Button :outlined="enrollMode !== 'photo'" size="small" severity="secondary" @click="enrollMode = 'photo'">
            Upload a photo
          </Button>
        </div>

        <EnrollFromClipPicker v-if="enrollMode === 'clip'" v-model:selected-frames="selectedClipFrames" />

        <div v-else class="photo-picker">
          <FileUpload
            mode="basic"
            :auto="false"
            custom-upload
            accept="image/*"
            choose-label="Choose photo…"
            :disabled="!available"
            @select="onFileSelect"
          />
          <img v-if="previewUrl" :src="previewUrl" alt="Enrollment preview" class="preview-thumb" />
          <Button v-if="selectedFile" text size="small" severity="secondary" @click="clearSelection">Clear</Button>
        </div>

        <div class="enroll-form-row">
          <label for="biometrics-name" class="field-label">Name</label>
          <InputText id="biometrics-name" v-model="name" placeholder="e.g. Brian" :disabled="!available" />
        </div>

        <div class="enroll-form-row approved-row">
          <ToggleSwitch v-model="approvedOnEnroll" input-id="biometrics-approved-on-enroll" :disabled="!available" />
          <label for="biometrics-approved-on-enroll" class="field-label"
            >Approve immediately (bypass alerts for this person)</label
          >
        </div>

        <Button class="enroll-submit-btn" :disabled="!available || enrolling" :loading="enrolling" @click="enroll">
          {{
            enrolling
              ? 'Enrolling…'
              : enrollMode === 'clip' && selectedClipFrames.length
                ? `+ Enroll ${selectedClipFrames.length} selected frame(s)`
                : '+ Enroll'
          }}
        </Button>
      </template>
    </Card>

    <div v-if="loading" class="muted-note">Loading…</div>
    <div v-else-if="!groupedPeople.length" class="muted-note">No one enrolled yet.</div>
    <div v-else class="people-grid">
      <div v-for="group in groupedPeople" :key="group.name" class="person-card">
        <Card>
          <template #content>
            <div class="person-card-header">
              <template v-if="editingName === group.name">
                <InputText
                  v-model="editingNameValue"
                  size="small"
                  class="rename-input"
                  @keyup.enter="saveEdit(group)"
                />
                <Button text size="small" @click="saveEdit(group)">Save</Button>
                <Button text size="small" severity="secondary" @click="cancelEdit">Cancel</Button>
              </template>
              <template v-else>
                <strong class="person-name">{{ group.name }}</strong>
                <Button text size="small" severity="secondary" title="Rename" @click="startEdit(group)">✎</Button>
              </template>
            </div>
            <div class="person-card-meta">
              <Tag :severity="group.approved ? 'success' : 'secondary'">
                {{ group.mixedApproval ? 'Partially approved' : group.approved ? 'Approved' : 'Not approved' }}
              </Tag>
              <span class="person-photo-count"
                >{{ group.photoCount }} photo{{ group.photoCount === 1 ? '' : 's' }}</span
              >
              <span class="person-created">Enrolled {{ group.createdAt.slice(0, 10) }}</span>
            </div>
            <div class="person-card-actions">
              <div class="approved-row">
                <ToggleSwitch
                  :model-value="group.approved"
                  :input-id="`biometrics-approved-${group.name}`"
                  @update:model-value="toggleApproved(group, $event)"
                />
                <label :for="`biometrics-approved-${group.name}`" class="field-label">Approved for bypass</label>
              </div>
              <Button text size="small" severity="danger" @click="remove(group)">Remove</Button>
            </div>
          </template>
        </Card>
      </div>
    </div>

    <p v-if="groupedPeople.length" class="muted-note approved-summary">
      {{ approvedCount }} of {{ groupedPeople.length }} enrolled
      {{ groupedPeople.length === 1 ? 'person is' : 'people are' }} approved for the suspicious-flag bypass.
    </p>
  </div>
</template>

<style scoped>
.biometrics-page {
  padding: 1.75rem;
  max-width: 900px;
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

.privacy-banner,
.safety-banner {
  margin-bottom: 0.9rem;
}

.enroll-card {
  margin-bottom: 1.5rem;
}

.mode-toggle {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 0.9rem;
  flex-wrap: wrap;
}

.enroll-form-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.9rem;
}

.enroll-submit-btn {
  margin-top: 0.9rem;
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
}

.photo-picker {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.preview-thumb {
  width: 48px;
  height: 48px;
  object-fit: cover;
  border-radius: 50%;
  border: 1px solid var(--border, rgba(255, 255, 255, 0.15));
}

.approved-row {
  display: flex;
  flex-direction: row;
  align-items: center;
  gap: 0.5rem;
}

.people-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(260px, 100%), 1fr));
  gap: 1rem;
}

.person-card-header {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.5rem;
}

.person-name {
  font-size: 1rem;
  flex: 1;
}

.rename-input {
  flex: 1;
}

.person-card-meta {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.7rem;
  flex-wrap: wrap;
}

.person-photo-count,
.person-created {
  font-size: 0.76rem;
  color: var(--muted);
}

.person-card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
  padding: 0.5rem 0;
}

.approved-summary {
  margin-top: 1rem;
}
</style>
