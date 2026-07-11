<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import videojs from 'video.js'
import type Player from 'video.js/dist/types/player'
import 'video.js/dist/video-js.css'
import { clipStreamUrl, getClip, setClipTags, starClip } from '../../api/clips'
import { fmtDur, fmtRelative, fmtSize, fmtTs } from '../../api/constants'
import type { ClipDetail } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useConfirmStore } from '../../stores/confirm'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useToastStore } from '../../stores/toast'
import AppIcon from '../icons/AppIcon.vue'
import ClipAiPanel from './ClipAiPanel.vue'

const props = defineProps<{
  clipId: string | null
  aiEnabled: boolean
  promptDebugEnabled: boolean
}>()
const emit = defineEmits<{
  close: []
  nav: [dir: number]
  deleted: [id: string]
  starred: [id: string, starred: boolean]
}>()

const toast = useToastStore()
const confirmStore = useConfirmStore()
const promptOverlay = usePromptOverlayStore()
const confirm = useConfirm()

const videoEl = ref<HTMLVideoElement | null>(null)
let player: Player | null = null

const clip = ref<ClipDetail | null>(null)
const starred = ref(false)
const currentTags = ref<string[]>([])
const tagInput = ref('')
const theater = ref(false)
const autoplayNext = ref(false)
const loopClip = ref(false)

function ensurePlayer(): Player {
  if (player) return player
  player = videojs(videoEl.value!, {
    fluid: true,
    responsive: true,
    controls: true,
    preload: 'auto',
    playbackRates: [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2],
    enableSmoothSeeking: true,
    html5: {
      vhs: { overrideNative: false },
      nativeVideoTracks: true,
      nativeAudioTracks: true,
      nativeTextTracks: true,
    },
    controlBar: {
      skipButtons: { forward: 10, backward: 10 },
      pictureInPictureToggle: true,
    },
    userActions: { hotkeys: false },
  })
  player.on('ended', () => {
    if (autoplayNext.value) emit('nav', 1)
  })
  return player
}

async function load(id: string) {
  try {
    const c = await getClip(id)
    clip.value = c
    currentTags.value = [...(c.tags || [])]
    starred.value = c.starred
    const p = ensurePlayer()
    p.src([{ src: clipStreamUrl(id), type: 'video/mp4' }])
    p.load()
    p.play()?.catch(() => {})
  } catch {
    toast.show('Failed to load clip', true)
  }
}

watch(
  () => props.clipId,
  (id) => {
    if (id) {
      void load(id)
    } else if (player) {
      player.pause()
      player.src('')
    }
  },
)

function downloadName(): string {
  if (!clip.value) return 'clip.mp4'
  return `${clip.value.camera}_${(clip.value.timestamp || '').replace(/[:.]/g, '-')}.mp4`
}

async function toggleStar() {
  if (!props.clipId) return
  const next = !starred.value
  await starClip(props.clipId, next)
  starred.value = next
  toast.show(next ? 'Starred ★' : 'Unstarred')
  emit('starred', props.clipId, next)
}

async function handleDelete() {
  if (!props.clipId) return
  if (!(await confirm('Delete this clip permanently?'))) return
  emit('deleted', props.clipId)
}

async function copyPath() {
  const path = clip.value?.file_path
  if (!path) return
  try {
    await navigator.clipboard.writeText(path)
    toast.show('File path copied')
  } catch {
    toast.show(path, true)
  }
}

function toggleTheater() {
  theater.value = !theater.value
  player?.fluid(!theater.value)
}

async function saveTags() {
  if (!props.clipId) return
  await setClipTags(props.clipId, currentTags.value)
}

async function onTagInputKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  const v = tagInput.value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, '')
  tagInput.value = ''
  if (v && !currentTags.value.includes(v)) {
    currentTags.value.push(v)
    await saveTags()
  }
}

async function removeTag(tag: string) {
  currentTags.value = currentTags.value.filter((t) => t !== tag)
  await saveTags()
}

watch(loopClip, (val) => player?.loop(val))

function onKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement
  if (target.tagName === 'INPUT') return
  if (e.key === 'Escape') {
    if (promptOverlay.open || confirmStore.open) return
    if (props.clipId) emit('close')
    return
  }
  if (!props.clipId || !player) return
  switch (e.key) {
    case ' ':
      e.preventDefault()
      if (player.paused()) player.play()
      else player.pause()
      break
    case 'ArrowLeft':
      e.preventDefault()
      player.currentTime(Math.max(0, (player.currentTime() ?? 0) - 10))
      break
    case 'ArrowRight':
      e.preventDefault()
      player.currentTime(Math.min(player.duration() || 0, (player.currentTime() ?? 0) + 10))
      break
    case 'f':
    case 'F':
      player.requestFullscreen()
      break
    case 'm':
    case 'M':
      player.muted(!player.muted())
      break
    case 'ArrowUp':
      e.preventDefault()
      emit('nav', -1)
      break
    case 'ArrowDown':
      e.preventDefault()
      emit('nav', 1)
      break
    case 'l':
    case 'L':
      loopClip.value = !loopClip.value
      toast.show(loopClip.value ? 'Loop ON' : 'Loop OFF')
      break
  }
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  // Normally clipId starts null and load() is driven entirely by the watch
  // above, but if this component is ever mounted with clipId already set,
  // the watch (which only reacts to changes) would miss it — videoEl isn't
  // bound to the DOM until after mount, so this can't happen inside setup().
  if (props.clipId) void load(props.clipId)
})
onUnmounted(() => {
  document.removeEventListener('keydown', onKeydown)
  player?.dispose()
})
</script>

<template>
  <div class="modal-bg" :class="{ open: !!clipId }" @click.self="emit('close')">
    <div class="modal" :class="{ theater }">
      <button class="modal-close" title="Close (Esc)" @click="emit('close')">
        <AppIcon name="close" />
      </button>
      <div class="video-wrap">
        <video ref="videoEl" class="video-js vjs-big-play-centered" preload="auto" playsinline>
          <p class="vjs-no-js">JavaScript is required to play videos.</p>
        </video>
        <div class="vid-nav">
          <button class="vid-nav-btn" title="Previous (↑)" @click="emit('nav', -1)">‹</button>
          <button class="vid-nav-btn" title="Next (↓)" @click="emit('nav', 1)">›</button>
        </div>
      </div>
      <div class="modal-body">
        <div class="modal-title">{{ clip ? `${clip.camera} — ${fmtTs(clip.timestamp)}` : '' }}</div>
        <div v-if="clip" class="meta-grid">
          <div>Camera</div>
          <span>{{ clip.camera }}</span>
          <div>Recorded</div>
          <span>{{ fmtTs(clip.timestamp) }}</span>
          <div>Duration</div>
          <span>{{ fmtDur(clip.duration) || '—' }}</span>
          <div>Size</div>
          <span>{{ fmtSize(clip.size_bytes) || '—' }}</span>
          <div>Source</div>
          <span>{{ clip.source || '—' }}</span>
          <div>Added</div>
          <span>{{ fmtRelative(clip.downloaded_at) }}</span>
        </div>
        <div class="modal-actions">
          <button class="btn sm outline" :style="starred ? 'color: var(--starred)' : ''" @click="toggleStar">
            {{ starred ? '★ Starred' : '☆ Star' }}
          </button>
          <a v-if="clipId" class="btn sm ghost" :href="clipStreamUrl(clipId)" :download="downloadName()">⬇ Download</a>
          <button class="btn sm ghost" @click="copyPath">📋 Path</button>
          <button class="btn sm ghost" title="Theater mode" @click="toggleTheater">
            {{ theater ? '⊡ Normal' : '⊞ Theater' }}
          </button>
          <button class="btn sm danger" style="margin-left: auto" @click="handleDelete">🗑 Delete</button>
        </div>
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.3rem">
            <label for="clip-tag-input" class="sr-only">Add tag</label>
            <input
              id="clip-tag-input"
              v-model="tagInput"
              class="tag-input"
              placeholder="Add tag + Enter"
              @keydown="onTagInputKeydown"
            />
            <span style="font-size: 0.72rem; color: var(--muted)">
              <span class="kbd">Space</span> play &nbsp; <span class="kbd">←→</span> ±10s &nbsp;
              <span class="kbd">F</span> full &nbsp; <span class="kbd">M</span> mute &nbsp;
              <span class="kbd">↑↓</span> prev/next
            </span>
          </div>
          <div class="tag-list">
            <span v-for="tag in currentTags" :key="tag" class="tag-item"
              >{{ tag }}<span class="rm" @click="removeTag(tag)">×</span></span
            >
          </div>
          <div class="modal-options">
            <label><input v-model="autoplayNext" type="checkbox" /> Auto-play next clip</label>
            <label><input v-model="loopClip" type="checkbox" /> Loop</label>
          </div>
          <ClipAiPanel
            v-if="aiEnabled && clipId"
            :key="clipId"
            :clip-id="clipId"
            :prompt-debug-enabled="promptDebugEnabled"
          />
        </div>
      </div>
    </div>
  </div>
</template>
