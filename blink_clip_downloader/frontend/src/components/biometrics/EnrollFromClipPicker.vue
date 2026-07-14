<script setup lang="ts">
import { ref, watch } from 'vue'
import Message from 'primevue/message'
import Select from 'primevue/select'
import { clipThumbUrl, getCameras, getClipFrames, listClips } from '../../api/clips'
import type { CameraStat, ClipListItem } from '../../api/types'

const selectedFrames = defineModel<string[]>('selectedFrames', { required: true })

const cameras = ref<CameraStat[]>([])
const selectedCamera = ref('')
const loadingCameras = ref(true)

const recentClips = ref<ClipListItem[]>([])
const selectedClipId = ref('')
const loadingClips = ref(false)

const frames = ref<string[]>([])
const loadingFrames = ref(false)
const framesError = ref(false)

async function loadCameras() {
  loadingCameras.value = true
  try {
    cameras.value = await getCameras()
    if (cameras.value.length) selectedCamera.value = cameras.value[0].camera
  } finally {
    loadingCameras.value = false
  }
}
loadCameras()

// Rapid camera/clip switching can fire loadClips/loadFrames again before an
// earlier call's response has resolved, letting a stale, slower response
// overwrite a newer selection's data — mirrors the request-sequencing guard
// in LibraryPage.vue. Each call captures its own token and only applies its
// result if it's still the most recently fired request by the time it
// resolves.
let clipsSeq = 0

async function loadClips() {
  if (!selectedCamera.value) return
  const seq = ++clipsSeq
  loadingClips.value = true
  recentClips.value = []
  selectedClipId.value = ''
  frames.value = []
  selectedFrames.value = []
  try {
    const result = await listClips({ camera: selectedCamera.value, limit: 8, sort: 'newest' })
    if (seq !== clipsSeq) return
    recentClips.value = result
    if (recentClips.value.length) selectedClipId.value = recentClips.value[0].id
  } finally {
    if (seq === clipsSeq) loadingClips.value = false
  }
}
// Setting selectedCamera.value inside loadCameras() (once cameras resolve)
// already triggers this watcher on its own — a second watch(cameras, ...)
// would fire again for the same initial selection and double the clips
// fetch, so this one watcher is deliberately the only trigger.
watch(selectedCamera, loadClips)

let framesSeq = 0

async function loadFrames() {
  if (!selectedClipId.value) return
  const seq = ++framesSeq
  loadingFrames.value = true
  framesError.value = false
  frames.value = []
  selectedFrames.value = []
  try {
    const result = await getClipFrames(selectedClipId.value, 8)
    if (seq !== framesSeq) return
    frames.value = result.frames
  } catch {
    if (seq === framesSeq) framesError.value = true
  } finally {
    if (seq === framesSeq) loadingFrames.value = false
  }
}
watch(selectedClipId, loadFrames)

function toggleFrame(frame: string) {
  const idx = selectedFrames.value.indexOf(frame)
  if (idx === -1) selectedFrames.value = [...selectedFrames.value, frame]
  else selectedFrames.value = selectedFrames.value.filter((f) => f !== frame)
}
</script>

<template>
  <div class="enroll-from-clip">
    <div class="picker-row">
      <label for="biometrics-camera-select" class="field-label">Camera</label>
      <Select
        id="biometrics-camera-select"
        v-model="selectedCamera"
        size="small"
        :options="cameras"
        option-label="camera"
        option-value="camera"
        :disabled="loadingCameras || !cameras.length"
        placeholder="Select a camera"
        class="camera-select"
      />
    </div>

    <Message v-if="!loadingCameras && !cameras.length" severity="warn" size="small" :closable="false">
      No cameras found yet — download at least one clip first.
    </Message>

    <div v-if="selectedCamera" class="clip-strip">
      <div v-if="loadingClips" class="muted-note">Loading recent clips…</div>
      <Message v-else-if="!recentClips.length" severity="warn" size="small" :closable="false">
        No clips yet for this camera.
      </Message>
      <div v-else class="thumb-strip">
        <button
          v-for="clip in recentClips"
          :key="clip.id"
          type="button"
          class="thumb-strip-item"
          :class="{ active: clip.id === selectedClipId }"
          @click="selectedClipId = clip.id"
        >
          <img :src="clipThumbUrl(clip.id)" alt="" loading="lazy" />
        </button>
      </div>
    </div>

    <div v-if="selectedClipId" class="frame-grid-wrap">
      <div v-if="loadingFrames" class="muted-note">Extracting frames from this clip…</div>
      <Message v-else-if="framesError" severity="error" size="small" :closable="false"
        >Failed to extract frames.</Message
      >
      <Message v-else-if="!frames.length" severity="warn" size="small" :closable="false">
        No frames could be extracted from this clip.
      </Message>
      <template v-else>
        <p class="field-label">Click every frame that shows the face clearly (you can pick more than one):</p>
        <div class="frame-grid">
          <button
            v-for="(frame, i) in frames"
            :key="i"
            type="button"
            class="frame-item"
            :class="{ selected: selectedFrames.includes(frame) }"
            @click="toggleFrame(frame)"
          >
            <img :src="frame" alt="" />
            <span v-if="selectedFrames.includes(frame)" class="frame-check">✓</span>
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.enroll-from-clip {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}

.picker-row {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.camera-select {
  max-width: 260px;
}

.field-label {
  font-size: 0.8rem;
  color: var(--muted);
}

.thumb-strip {
  display: flex;
  gap: 0.4rem;
  overflow-x: auto;
  padding-bottom: 0.2rem;
}

.thumb-strip-item {
  flex: 0 0 auto;
  width: 64px;
  height: 48px;
  padding: 0;
  border-radius: 0.3rem;
  border: 2px solid transparent;
  overflow: hidden;
  cursor: pointer;
  background: var(--card2, rgba(255, 255, 255, 0.04));
}

.thumb-strip-item.active {
  border-color: var(--accent, #5b9cf6);
}

.thumb-strip-item img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.frame-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(110px, 100%), 1fr));
  gap: 0.5rem;
}

.frame-item {
  position: relative;
  padding: 0;
  border-radius: 0.35rem;
  border: 2px solid transparent;
  overflow: hidden;
  cursor: pointer;
  background: var(--card2, rgba(255, 255, 255, 0.04));
}

.frame-item.selected {
  border-color: var(--accent, #5b9cf6);
}

.frame-item img {
  width: 100%;
  display: block;
}

.frame-check {
  position: absolute;
  top: 4px;
  right: 4px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--accent, #5b9cf6);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 0.8rem;
}

.muted-note {
  font-size: 0.78rem;
  color: var(--muted);
}
</style>
