<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import videojs from 'video.js'
import type Player from 'video.js/dist/types/player'
import 'video.js/dist/video-js.css'
import Button from 'primevue/button'
import InputText from 'primevue/inputtext'
import { clipStreamUrl, clipThumbUrl, getClip, setClipTags, starClip } from '../../api/clips'
import { fmtDur, fmtRelative, fmtSize, fmtTs } from '../../api/constants'
import type { ClipDetail } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useConfirmStore } from '../../stores/confirm'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import AppIcon from '../icons/AppIcon.vue'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import ClipDetectionOverlay from './ClipDetectionOverlay.vue'
import ClipAiPanel from './ClipAiPanel.vue'

const props = defineProps<{
  clipId: string | null
  aiEnabled: boolean
  promptDebugEnabled: boolean
  availableTags?: string[]
  /** Clip-relative seconds to open at, when the caller knows which moment
   *  matters. Used once per clip, on the first metadata load, so the viewer
   *  can still scrub freely afterwards. */
  startAt?: number | null
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
const refresh = useRefreshStore()
const confirm = useConfirm()

const videoEl = ref<HTMLVideoElement | null>(null)
let player: Player | null = null

const clip = ref<ClipDetail | null>(null)
const starred = ref(false)
const currentTags = ref<string[]>([])
const tagInput = ref('')
const tagInputFocused = ref(false)
// Typeahead suggestions for the "Add tag" box: existing tags from other
// clips (passed down from LibraryPage, which already keeps its own copy
// fresh via the shared refresh signal — see saveTags() below) that aren't
// already on this clip, narrowed by whatever's been typed so far.
const tagSuggestions = computed(() => {
  const q = tagInput.value.trim().toLowerCase()
  return (props.availableTags ?? [])
    .filter((t) => !currentTags.value.includes(t))
    .filter((t) => !q || t.toLowerCase().includes(q))
    .slice(0, 8)
})
const showTagSuggestions = computed(() => tagInputFocused.value && tagSuggestions.value.length > 0)
const theater = ref(false)
const showOverlay = ref(false)
const currentTime = ref(0)
const autoplayNext = ref(false)
const loopClip = ref(false)
// A handful of real Blink clips (source: "snapshot" in particular) download
// as technically-valid but near-static MP4s (e.g. 5 frames across several
// seconds) that some browsers' video decoders refuse to play even though
// the file itself is fine — ffprobe accepts them without complaint. Video.js
// surfaces that as a raw, unstyled "media could not be loaded" error inside
// the player chrome; fall back to the clip's own thumbnail instead of
// leaking that browser-level message.
const videoError = ref(false)
// True from the moment a clip is asked for until its own first frame is
// decoded. Until then the modal has nothing real to show — the previous
// version simply rendered a black void where the video would be and no
// title or metadata at all, then reflowed everything downwards once the
// request landed. Both placeholders below are driven by this.
const videoReady = ref(false)

/** Label/value pairs for the metadata grid, with `null` values until the
 *  clip's details arrive. The grid renders its six rows either way, so the
 *  actions and tags below it sit where they will stay rather than jumping
 *  down the moment the request resolves. */
const metaRows = computed(() => {
  const c = clip.value
  return [
    { label: 'Camera', value: c ? c.camera : null },
    { label: 'Recorded', value: c ? fmtTs(c.timestamp) : null },
    { label: 'Duration', value: c ? fmtDur(c.duration) || '—' : null },
    { label: 'Size', value: c ? fmtSize(c.size_bytes) || '—' : null },
    { label: 'Source', value: c ? c.source || '—' : null },
    { label: 'Added', value: c ? fmtRelative(c.downloaded_at) : null },
  ]
})

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
  // Drives the optional detection overlay. Tracked on the player rather
  // than inside the overlay so the overlay stays a pure presentational
  // component with nothing to mock in its own tests.
  // Bound to the local instance, not the module-level `player`, so the
  // handler needs no null check for something that cannot be null inside
  // the call that just created it.
  const instance = player
  instance.on('timeupdate', () => {
    // video.js types currentTime() as possibly undefined (it is, before
    // metadata loads), and an undefined here would blank the overlay
    // rather than leave it where it was.
    currentTime.value = instance.currentTime() ?? 0
  })
  player.on('ended', () => {
    if (autoplayNext.value) emit('nav', 1)
  })
  player.on('error', () => {
    videoError.value = true
  })
  // Whichever of these lands first retires the poster placeholder below.
  // 'playing' alone would strand it forever when the browser refuses to
  // autoplay, which leaves the clip sitting on its big play button —
  // ready, just not started.
  player.on('loadeddata', () => {
    videoReady.value = true
  })
  player.on('playing', () => {
    videoReady.value = true
  })
  return player
}

// Rapid prev/next navigation (clicks or arrow keys) can fire load() again
// before an earlier call's getClip() resolves, with no inherent ordering —
// whichever response arrives last would otherwise overwrite the player/
// metadata regardless of which clip is actually selected by then. Mirrors
// LibraryPage.vue's requestSeq pattern.
let requestSeq = 0

async function load(id: string) {
  const seq = ++requestSeq
  videoError.value = false
  videoReady.value = false
  // A clip's title, metadata, star and tags belong to that clip alone.
  // Leaving the previous one's on screen while this one loads showed the
  // wrong values outright — and, for the star, would have toggled *from*
  // them, writing the previous clip's state onto this one.
  clip.value = null
  currentTags.value = []
  starred.value = false
  try {
    const c = await getClip(id)
    if (seq !== requestSeq) return
    clip.value = c
    currentTags.value = [...(c.tags || [])]
    starred.value = c.starred
    const p = ensurePlayer()
    p.src([{ src: clipStreamUrl(id), type: 'video/mp4' }])
    // Seeking before the source reports a duration is silently ignored, so
    // it waits for metadata — `once`, so a viewer who scrubs elsewhere and
    // lets the clip loop is not yanked back to the same second every time.
    const at = props.startAt
    if (at != null && at > 0) {
      p.one('loadedmetadata', () => {
        if (seq !== requestSeq) return
        p.currentTime(at)
      })
    }
    p.load()
    p.play()?.catch(() => {})
  } catch {
    if (seq === requestSeq) toast.show('Failed to load clip', true)
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
  try {
    await starClip(props.clipId, next)
  } catch {
    // Without this the click simply did nothing: no star, no message, no
    // hint that the request had failed at all.
    toast.show('Could not update the star', true)
    return
  }
  starred.value = next
  toast.show(next ? 'Starred ★' : 'Unstarred')
  emit('starred', props.clipId, next)
}

async function handleDelete() {
  // Captured before the await: the modal can be showing a different clip by
  // the time the dialog is answered (prev/next, or another tab opening one
  // through the clip-viewer store), and deleting a clip the prompt never
  // named is not recoverable.
  const id = props.clipId
  if (!id) return
  if (!(await confirm('Delete this clip permanently?'))) return
  if (props.clipId !== id) return
  emit('deleted', id)
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

/** Persist the tag list, restoring *previous* if the request fails.
 *
 *  Both callers edit `currentTags` first so the chip appears or disappears
 *  immediately. Leaving that optimistic edit in place after a failed save
 *  was the worst version of this: the tag looked saved, was not, and
 *  vanished on the next reload with nothing ever having said so. */
async function saveTags(previous: string[]) {
  if (!props.clipId) return
  try {
    await setClipTags(props.clipId, currentTags.value)
  } catch {
    currentTags.value = previous
    toast.show('Could not save tags', true)
    return
  }
  // The Library tab's tag filter dropdown lists every distinct tag in use —
  // bump the shared refresh signal so it picks up a newly-added or just-
  // removed tag without the user having to reload the page.
  refresh.bump()
}

async function addTag(raw: string) {
  const v = raw
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '-')
    .replace(/[^a-z0-9_-]/g, '')
  if (v && !currentTags.value.includes(v)) {
    const previous = [...currentTags.value]
    currentTags.value.push(v)
    await saveTags(previous)
  }
}

async function onTagInputKeydown(e: KeyboardEvent) {
  if (e.key !== 'Enter') return
  const v = tagInput.value
  tagInput.value = ''
  await addTag(v)
}

async function selectTagSuggestion(tag: string) {
  tagInput.value = ''
  await addTag(tag)
}

async function removeTag(tag: string) {
  const id = props.clipId
  if (!(await confirm(`Remove tag "${tag}" from this clip?`))) return
  // Same reason handleDelete captures its id: currentTags has already been
  // reloaded for whichever clip is showing now, so saving it would edit
  // that clip's tags instead of the one the prompt named.
  if (props.clipId !== id) return
  const previous = [...currentTags.value]
  currentTags.value = currentTags.value.filter((t) => t !== tag)
  await saveTags(previous)
}

watch(loopClip, (val) => player?.loop(val))

/** True while something is stacked on top of the clip modal.
 *
 *  Both keyboard paths below defer to it: Escape belongs to whatever is on
 *  top, and the playback/navigation shortcuts would otherwise drive the
 *  modal *behind* an open dialog — ArrowUp/ArrowDown swapping the clip out
 *  from under a "Delete this clip permanently?" prompt that named the
 *  previous one being the case that actually cost something.
 */
function overlayOnTop(): boolean {
  return promptOverlay.open || confirmStore.open
}

function onEscape(target: HTMLElement, isTextInput: boolean) {
  // Escape blurs a focused input (the tag/feedback-note fields) rather
  // than being silently swallowed, matching the typical "Escape blurs the
  // input" convention instead of doing nothing.
  if (isTextInput) {
    target.blur()
    return
  }
  if (overlayOnTop()) return
  if (props.clipId) emit('close')
}

function onKeydown(e: KeyboardEvent) {
  const target = e.target as HTMLElement
  const isTextInput = target.tagName === 'INPUT'
  if (e.key === 'Escape') {
    onEscape(target, isTextInput)
    return
  }
  if (isTextInput) return
  if (overlayOnTop()) return
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
      player.requestFullscreen().catch(() => toast.show('Fullscreen not available', true))
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
  <!-- .self on the Escape handler, exactly like the click one beside it:
       Escape raised from *inside* the modal is already handled by the
       document-level onKeydown above, which has guards this backdrop does
       not (a focused text input blurs instead of closing; an open confirm
       dialog or prompt overlay takes precedence). Without .self a bubbled
       Escape would reach here first and close the modal unconditionally,
       losing all three. -->
  <div class="modal-bg" :class="{ open: !!clipId }" @click.self="emit('close')" @keydown.escape.self="emit('close')">
    <div class="modal" :class="{ theater }">
      <button type="button" class="modal-close" title="Close (Esc)" aria-label="Close" @click="emit('close')">
        <AppIcon name="close" />
      </button>
      <div class="video-wrap" :class="{ 'video-wrap-loading': !videoReady && !videoError }">
        <!--
          Video.js takes over the <video> tag on init and wraps it in its
          own outer chrome div (controls, big-play button, and — the reason
          this wrapper exists — its own error overlay), which ends up as a
          *sibling* structure Vue's own :class binding on the original
          <video> tag can't reach. Hiding this dedicated wrapper div (which
          Video.js only ever adds *into*, never replaces) reliably hides
          all of that instead.
        -->
        <div class="video-js-wrap" :class="{ 'video-hidden': videoError }">
          <video ref="videoEl" class="video-js vjs-big-play-centered" preload="auto" playsinline>
            <p class="vjs-no-js">JavaScript is required to play videos.</p>
          </video>
        </div>
        <!-- Stands in for the picture until the first frame is decoded.
             The clip's own thumbnail is almost always already in the
             browser cache (it is what was clicked in the grid), so this
             paints immediately, in the right aspect ratio, instead of the
             black void the modal used to open as. -->
        <div v-if="clipId && !videoReady && !videoError" class="video-poster">
          <img :src="clipThumbUrl(clipId)" alt="" class="video-poster-img" />
          <LoadingIndicator class="video-poster-msg" label="Loading clip…" />
        </div>
        <div v-if="videoError && clipId" class="video-fallback">
          <img :src="clipThumbUrl(clipId)" alt="" class="video-fallback-thumb" />
          <div class="video-fallback-msg">
            <p>Video preview isn't available for this clip.</p>
            <Button as="a" :href="clipStreamUrl(clipId)" :download="downloadName()" size="small" outlined
              >⬇ Download instead</Button
            >
          </div>
        </div>
        <ClipDetectionOverlay
          v-if="showOverlay && clipId && !videoError"
          :clip-id="clipId"
          :current-time="currentTime"
        />
        <div class="vid-nav">
          <button type="button" class="vid-nav-btn" title="Previous (↑)" @click="emit('nav', -1)">‹</button>
          <button type="button" class="vid-nav-btn" title="Next (↓)" @click="emit('nav', 1)">›</button>
        </div>
      </div>
      <div class="modal-body">
        <div class="modal-title">
          <template v-if="clip">{{ `${clip.camera} — ${fmtTs(clip.timestamp)}` }}</template>
          <span v-else-if="clipId" class="skel skel-title" aria-hidden="true"></span>
        </div>
        <div v-if="clipId" class="meta-grid">
          <template v-for="meta in metaRows" :key="meta.label">
            <div>{{ meta.label }}</div>
            <span v-if="meta.value !== null">{{ meta.value }}</span>
            <span v-else class="skel" aria-hidden="true"></span>
          </template>
        </div>
        <div class="modal-actions">
          <Button
            size="small"
            severity="secondary"
            :outlined="!showOverlay"
            title="Draw the object detector's boxes over the video"
            @click="showOverlay = !showOverlay"
          >
            {{ showOverlay ? '⬚ Boxes on' : '⬚ Boxes' }}
          </Button>
          <Button size="small" outlined :style="starred ? 'color: var(--starred)' : ''" @click="toggleStar">
            {{ starred ? '★ Starred' : '☆ Star' }}
          </Button>
          <Button
            v-if="clipId"
            as="a"
            :href="clipStreamUrl(clipId)"
            :download="downloadName()"
            size="small"
            severity="secondary"
            outlined
            >⬇ Download</Button
          >
          <Button size="small" severity="secondary" outlined @click="copyPath">📋 Path</Button>
          <Button size="small" severity="secondary" outlined title="Theater mode" @click="toggleTheater">
            {{ theater ? '⊡ Normal' : '⊞ Theater' }}
          </Button>
          <Button size="small" severity="danger" style="margin-left: auto" @click="handleDelete">🗑 Delete</Button>
        </div>
        <div>
          <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.3rem">
            <div style="position: relative">
              <label for="clip-tag-input" class="sr-only">Add tag</label>
              <InputText
                id="clip-tag-input"
                v-model="tagInput"
                class="tag-input"
                placeholder="Add tag + Enter"
                autocomplete="off"
                @keydown="onTagInputKeydown"
                @focus="tagInputFocused = true"
                @blur="tagInputFocused = false"
              />
              <ul v-if="showTagSuggestions" class="tag-suggestions">
                <li v-for="s in tagSuggestions" :key="s" @mousedown.prevent="selectTagSuggestion(s)">
                  {{ s }}
                </li>
              </ul>
            </div>
            <span style="font-size: 0.72rem; color: var(--muted)">
              <span class="kbd">Space</span> play &nbsp; <span class="kbd">←→</span> ±10s &nbsp;
              <span class="kbd">F</span> full &nbsp; <span class="kbd">M</span> mute &nbsp;
              <span class="kbd">↑↓</span> prev/next
            </span>
          </div>
          <div class="tag-list">
            <span v-for="tag in currentTags" :key="tag" class="tag-item"
              >{{ tag
              }}<button type="button" class="rm" :aria-label="`Remove tag ${tag}`" @click="removeTag(tag)">
                ×
              </button></span
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
