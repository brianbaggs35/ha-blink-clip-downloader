<script setup lang="ts">
// Marking a new asset, or changing one: what it is, what it's called, and
// where it is on the camera's frame. The frame is the camera's saved
// reference frame when it has one — every asset on a camera is drawn over
// the same picture — or any recent clip's, which becomes the new reference.
import { computed, ref, useId, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import InputText from 'primevue/inputtext'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import Textarea from 'primevue/textarea'
import { createAsset, updateAsset } from '../../api/assets'
import { describeApiError } from '../../api/client'
import { clipThumbUrl, listClips } from '../../api/clips'
import type { AssetDraft, AssetLimits, AssetType, AssetZone, ClipListItem, ProtectedAsset } from '../../api/types'
import { ASSET_TYPES, assetTypeInfo } from './assetTypes'
import AssetZoneCanvas from './AssetZoneCanvas.vue'
import type { OverlayZone } from './assetZoneGeometry'

const props = defineProps<{
  visible: boolean
  camera: string
  /** The asset being changed, or null to mark a new one. */
  asset: ProtectedAsset | null
  /** The camera's other assets, drawn faintly so a new one can go beside them. */
  others: OverlayZone[]
  color: string
  /** Whether the camera already has a saved reference frame to draw on. */
  hasReferenceFrame: boolean
  snapshotUrl: string
  limits: AssetLimits
}>()
const emit = defineEmits<{ 'update:visible': [boolean]; saved: [ProtectedAsset] }>()

const REFERENCE = 'reference'

const name = ref('')
const assetType = ref<AssetType | null>(null)
const description = ref('')
const zone = ref<AssetZone | null>(null)
const tool = ref<'rect' | 'polygon'>('rect')
const frame = ref<string>(REFERENCE)
const clips = ref<ClipListItem[]>([])
const clipsLoading = ref(false)
const clipsFailed = ref(false)
const frameFailed = ref(false)
const stripFailed = ref<Record<string, boolean>>({})
const saving = ref(false)
const error = ref('')
const nameTouched = ref(false)

const ids = { name: useId(), type: useId(), description: useId() }

const toolOptions = [
  { label: 'Rectangle', value: 'rect' as const, icon: 'pi pi-stop' },
  { label: 'Freeform', value: 'polygon' as const, icon: 'pi pi-pencil' },
]

// Only the newest request's answer is applied: reopening the dialog on a
// different camera must not be overwritten by the previous camera's list.
let clipsSeq = 0

async function loadClips() {
  const seq = ++clipsSeq
  clipsLoading.value = true
  clipsFailed.value = false
  try {
    const result = await listClips({ camera: props.camera, limit: 8, sort: 'newest' })
    if (seq !== clipsSeq) return
    clips.value = result
    // No saved frame to fall back on: draw on the newest clip's, or on
    // nothing at all when there isn't one — never on a frame that doesn't
    // exist.
    if (frame.value !== REFERENCE && !result.some((c) => c.id === frame.value)) {
      frame.value = result[0]?.id ?? ''
    }
  } catch {
    if (seq === clipsSeq) clipsFailed.value = true
  } finally {
    if (seq === clipsSeq) clipsLoading.value = false
  }
}

function reset() {
  const asset = props.asset
  name.value = asset?.name ?? ''
  assetType.value = asset?.asset_type ?? null
  description.value = asset?.description ?? ''
  zone.value = asset ? asset.zone : null
  tool.value = asset?.zone.shape === 'polygon' ? 'polygon' : 'rect'
  frame.value = REFERENCE
  clips.value = []
  frameFailed.value = false
  stripFailed.value = {}
  error.value = ''
  nameTouched.value = false
  saving.value = false
}

watch(
  () => props.visible,
  (open) => {
    if (!open) return
    reset()
    // With no saved frame yet, the first recent clip's frame is the one to
    // draw on; loadClips picks it once the list arrives.
    if (!props.hasReferenceFrame) frame.value = ''
    void loadClips()
  },
  { immediate: true },
)

// Picking a type offers its usual name, until someone types their own.
watch(assetType, (next, previous) => {
  if (!next) return
  const previousDefault = previous ? assetTypeInfo(previous).defaultName : ''
  if (!name.value.trim() || name.value === previousDefault) name.value = assetTypeInfo(next).defaultName
})

const typeInfo = computed(() => (assetType.value ? assetTypeInfo(assetType.value) : null))
const frameSrc = computed(() => (frame.value === REFERENCE ? props.snapshotUrl : clipThumbUrl(frame.value)))
const hasFrame = computed(() => frame.value === REFERENCE || Boolean(frame.value))
const trimmedName = computed(() => name.value.trim())
const nameError = computed(() => (nameTouched.value && !trimmedName.value ? 'Give it a name.' : ''))

/** The asset as it would be saved, or null while anything is missing. */
const draft = computed<AssetDraft | null>(() =>
  trimmedName.value && assetType.value && zone.value
    ? {
        name: trimmedName.value,
        asset_type: assetType.value,
        description: description.value.trim(),
        zone: zone.value,
      }
    : null,
)
const canSave = computed(() => draft.value !== null && hasFrame.value && !saving.value)

/** What still stands between the form and saving, said plainly. */
const missing = computed(() => {
  const needs: string[] = []
  if (!assetType.value) needs.push('choose what it is')
  if (!trimmedName.value) needs.push('name it')
  if (!zone.value) needs.push('draw it on the frame')
  return needs
})

function selectFrame(id: string) {
  frame.value = id
  frameFailed.value = false
}

function close() {
  emit('update:visible', false)
}

// Also reached by Enter in the name field, where the form may well be
// incomplete or a save already on its way — so it checks rather than
// trusting the Save button's own disabled state.
async function save() {
  nameTouched.value = true
  const payload = draft.value
  if (!payload || !canSave.value) return
  saving.value = true
  error.value = ''
  const clipId = frame.value === REFERENCE ? undefined : frame.value
  try {
    const result = props.asset
      ? await updateAsset(props.asset.id, { ...payload, ...(clipId ? { clip_id: clipId } : {}) })
      : await createAsset(props.camera, payload, clipId)
    emit('saved', result.asset)
    close()
  } catch (err) {
    error.value = describeApiError(err, 'Could not save this asset.')
  } finally {
    saving.value = false
  }
}

const header = computed(() => (props.asset ? `Edit “${props.asset.name}”` : `Mark an asset on ${props.camera}`))
</script>

<template>
  <Dialog
    :visible="visible"
    modal
    :header="header"
    :draggable="false"
    :style="{ width: 'min(1000px, 96vw)' }"
    :breakpoints="{ '640px': '100vw' }"
    :pt="{ root: { class: 'asset-editor' } }"
    @update:visible="emit('update:visible', $event)"
  >
    <div class="editor-grid">
      <section class="editor-frame" aria-label="Where it is">
        <div class="frame-toolbar">
          <SelectButton
            v-model="tool"
            :options="toolOptions"
            option-label="label"
            option-value="value"
            :allow-empty="false"
            aria-label="Drawing tool"
            size="small"
          >
            <template #option="{ option }">
              <i :class="option.icon" aria-hidden="true" />
              <span>{{ option.label }}</span>
            </template>
          </SelectButton>
          <Button
            label="Clear"
            icon="pi pi-eraser"
            size="small"
            text
            severity="secondary"
            :disabled="!zone"
            @click="zone = null"
          />
        </div>

        <Message v-if="!hasReferenceFrame && clipsLoading" severity="secondary" size="small" :closable="false">
          Loading this camera's recent frames…
        </Message>
        <Message v-else-if="!hasFrame && clipsFailed" severity="error" size="small" :closable="false">
          Couldn't load this camera's recent clips.
          <Button label="Try again" size="small" text @click="loadClips" />
        </Message>
        <Message v-else-if="!hasFrame" severity="warn" size="small" :closable="false">
          No clips from this camera yet, so there's no frame to draw on. Come back once one has downloaded.
        </Message>
        <template v-else>
          <AssetZoneCanvas
            v-model="zone"
            :src="frameSrc"
            :alt="`Frame from ${camera}`"
            :tool="tool"
            :color="color"
            :others="others"
            @load="frameFailed = false"
            @error="frameFailed = true"
          />
          <p class="muted-note hint" aria-live="polite">
            <i class="pi pi-info-circle" aria-hidden="true" />
            {{
              tool === 'rect'
                ? 'Drag to draw a box around it. Drag the box or its corners to adjust it.'
                : 'Press, trace around it, and let go — the outline closes itself.'
            }}
          </p>
          <Message v-if="frameFailed" severity="warn" size="small" :closable="false">
            This frame couldn't be loaded. Pick another below.
          </Message>
        </template>

        <div v-if="hasReferenceFrame || clips.length" class="frame-strip-wrap">
          <span class="field-label">Frame to draw on</span>
          <div class="frame-strip" role="group" aria-label="Frame to draw on">
            <button
              v-if="hasReferenceFrame"
              type="button"
              class="frame-tile"
              :class="{ active: frame === REFERENCE }"
              :aria-pressed="frame === REFERENCE"
              aria-label="The saved frame this camera's assets are drawn on"
              @click="selectFrame(REFERENCE)"
            >
              <img :src="snapshotUrl" alt="" loading="lazy" />
              <span class="frame-tile-tag">Saved</span>
            </button>
            <button
              v-for="clip in clips"
              :key="clip.id"
              type="button"
              class="frame-tile"
              :class="{ active: frame === clip.id }"
              :aria-pressed="frame === clip.id"
              :aria-label="`Frame from the clip at ${new Date(clip.timestamp).toLocaleString()}`"
              @click="selectFrame(clip.id)"
            >
              <img
                v-if="!stripFailed[clip.id]"
                :src="clipThumbUrl(clip.id)"
                alt=""
                loading="lazy"
                @error="stripFailed = { ...stripFailed, [clip.id]: true }"
              />
              <i v-else class="pi pi-image frame-tile-missing" aria-hidden="true" />
            </button>
          </div>
          <p class="muted-note">The camera doesn't move, so a zone stays put when you switch frames.</p>
        </div>
      </section>

      <section class="editor-form" aria-label="What it is">
        <div class="field">
          <label :for="ids.type" class="field-label">What is it?</label>
          <Select
            v-model="assetType"
            :input-id="ids.type"
            :options="ASSET_TYPES"
            option-label="label"
            option-value="value"
            placeholder="Choose what it is"
            class="full-width"
          >
            <template #value="{ value, placeholder }">
              <span v-if="value" class="type-option">
                <i :class="assetTypeInfo(value).icon" aria-hidden="true" />
                {{ assetTypeInfo(value).label }}
              </span>
              <span v-else>{{ placeholder }}</span>
            </template>
            <template #option="{ option }">
              <span class="type-option">
                <i :class="option.icon" aria-hidden="true" />
                {{ option.label }}
              </span>
            </template>
          </Select>
          <p v-if="typeInfo" class="watched-for">
            <i class="pi pi-eye" aria-hidden="true" />
            <span>{{ typeInfo.watchedFor }}</span>
          </p>
        </div>

        <div class="field">
          <label :for="ids.name" class="field-label">Name</label>
          <InputText
            :id="ids.name"
            v-model="name"
            :maxlength="limits.name"
            :invalid="Boolean(nameError)"
            placeholder="e.g. Front door"
            class="full-width"
            @blur="nameTouched = true"
            @keydown.enter.prevent="save"
          />
          <div class="field-meta">
            <small v-if="nameError" class="field-error">{{ nameError }}</small>
            <small class="counter">{{ name.length }}/{{ limits.name }}</small>
          </div>
        </div>

        <div class="field">
          <label :for="ids.description" class="field-label"
            >What it looks like <span class="optional">(optional)</span></label
          >
          <Textarea
            :id="ids.description"
            v-model="description"
            :maxlength="limits.description"
            rows="2"
            auto-resize
            placeholder="e.g. blue door with a glass panel"
            class="full-width"
          />
          <small class="muted-note">Helps the AI pick it out in the frames.</small>
        </div>

        <Message v-if="error" severity="error" size="small" :closable="false">{{ error }}</Message>
      </section>
    </div>

    <template #footer>
      <div class="editor-footer">
        <span v-if="missing.length" class="muted-note footer-hint">To save, {{ missing.join(', then ') }}.</span>
        <div class="footer-buttons">
          <Button label="Cancel" severity="secondary" text :disabled="saving" @click="close" />
          <Button
            :label="asset ? 'Save changes' : 'Save asset'"
            icon="pi pi-check"
            :loading="saving"
            :disabled="!canSave"
            @click="save"
          />
        </div>
      </div>
    </template>
  </Dialog>
</template>

<style scoped>
.editor-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr);
  gap: 1.25rem;
  align-items: start;
}

.editor-frame,
.editor-form {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-width: 0;
}

.frame-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.frame-toolbar :deep(.p-togglebutton-content) {
  gap: 0.4rem;
}

.hint {
  display: flex;
  gap: 0.4rem;
  align-items: baseline;
}

.frame-strip-wrap {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.frame-strip {
  display: flex;
  gap: 0.45rem;
  overflow-x: auto;
  padding: 0.15rem 0.1rem 0.35rem;
  scroll-snap-type: x proximity;
}

.frame-tile {
  position: relative;
  flex: 0 0 auto;
  width: 76px;
  height: 52px;
  padding: 0;
  border: 2px solid var(--border-strong);
  border-radius: var(--radius-sm);
  overflow: hidden;
  background: var(--card2);
  cursor: pointer;
  scroll-snap-align: start;
}

.frame-tile:hover {
  border-color: var(--muted);
}

.frame-tile.active {
  border-color: var(--accent);
  box-shadow: 0 0 0 2px var(--accent-glow);
}

.frame-tile:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}

.frame-tile img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.frame-tile-tag {
  position: absolute;
  left: 0.2rem;
  bottom: 0.2rem;
  padding: 0 0.3rem;
  border-radius: 0.25rem;
  background: rgba(10, 10, 15, 0.8);
  color: #fff;
  font-size: 0.65rem;
  font-weight: 600;
}

.frame-tile-missing {
  color: var(--muted);
  line-height: 48px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field-label {
  font-size: 0.85rem;
  font-weight: 600;
}

.optional {
  font-weight: 400;
  color: var(--muted);
}

.full-width {
  width: 100%;
}

.field-meta {
  display: flex;
  justify-content: space-between;
  gap: 0.5rem;
}

.counter {
  margin-left: auto;
  color: var(--muted);
}

.field-error {
  color: var(--danger);
}

.type-option {
  display: inline-flex;
  align-items: center;
  gap: 0.5rem;
}

.watched-for {
  display: flex;
  gap: 0.45rem;
  align-items: baseline;
  margin: 0;
  padding: 0.55rem 0.7rem;
  border-radius: var(--radius-sm);
  background: var(--card2);
  border: 1px solid var(--border);
  font-size: 0.82rem;
  color: var(--text-dim);
}

.muted-note {
  margin: 0;
  color: var(--muted);
  font-size: 0.8rem;
}

.editor-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  flex-wrap: wrap;
  gap: 0.5rem 0.75rem;
  width: 100%;
}

.footer-hint {
  margin-right: auto;
}

.footer-buttons {
  display: flex;
  gap: 0.5rem;
  white-space: nowrap;
}

@media (max-width: 640px) {
  /* The hint takes its own line, so the buttons never wrap mid-label. */
  .footer-hint {
    flex-basis: 100%;
  }
}

@media (max-width: 760px) {
  .editor-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  /* On a phone the form reads first, then the frame to draw on. */
  .editor-form {
    order: -1;
  }
}
</style>

<style>
/* Not scoped: the dialog is teleported to <body>, outside this component's
   scope. On a phone the editor is the whole screen — drawing round a door
   with a fingertip needs every pixel of the frame, not a card floating in a
   margin. */
@media (max-width: 640px) {
  .p-dialog.asset-editor {
    width: 100vw !important;
    max-width: 100vw;
    height: 100dvh;
    max-height: 100dvh;
    margin: 0;
    border-radius: 0;
  }
}
</style>
