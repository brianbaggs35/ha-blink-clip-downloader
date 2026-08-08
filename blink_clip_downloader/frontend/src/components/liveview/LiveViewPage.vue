<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import videojs from 'video.js'
import type Player from 'video.js/dist/types/player'
import 'video.js/dist/video-js.css'
import Button from 'primevue/button'
import Message from 'primevue/message'
import SelectButton from 'primevue/selectbutton'
import {
  getLiveViewCameras,
  getLiveViewStatus,
  liveViewPlaylistUrl,
  sendLiveViewHeartbeat,
  startLiveView,
  stopLiveView,
} from '../../api/liveview'
import type { LiveViewStatus } from '../../api/types'
import { useToastStore } from '../../stores/toast'
import LoadingIndicator from '../layout/LoadingIndicator.vue'

// Two independent timers while a session is active: a status poll (drives
// the starting -> live transition, surfaces errors, and detects the server
// ending the session on its own — idle timeout, hard cap, or another tab
// switching cameras) and a heartbeat ping (keeps it alive; see
// LiveViewManager's idle_timeout in live_view.py).
const STATUS_POLL_INTERVAL_MS = 4000
const HEARTBEAT_INTERVAL_MS = 15000

const toast = useToastStore()

const loadingCameras = ref(true)
const cameras = ref<string[]>([])
const selectedCamera = ref<string | null>(null)
const status = ref<LiveViewStatus>({ active: false })
const starting = ref(false)

const videoEl = ref<HTMLVideoElement | null>(null)
let player: Player | null = null

let statusTimer: ReturnType<typeof setInterval> | undefined
let heartbeatTimer: ReturnType<typeof setInterval> | undefined

function ensurePlayer(): Player {
  if (player) return player
  player = videojs(videoEl.value!, {
    fluid: true,
    responsive: true,
    controls: true,
    autoplay: true,
    // Muted autoplay is reliably allowed across browsers; an unmute control
    // stays available in the control bar.
    muted: true,
    // Video.js's live-edge UI — appropriate since the HLS manifest never
    // gets an ENDLIST tag (see live_view.py's ffmpeg -hls_flags).
    liveui: true,
    html5: { vhs: { overrideNative: false } },
    controlBar: { pictureInPictureToggle: true },
  })
  player.on('error', () => toast.show('Live view playback error', true))
  return player
}

function stopTimers() {
  clearInterval(statusTimer)
  clearInterval(heartbeatTimer)
  statusTimer = undefined
  heartbeatTimer = undefined
}

async function sendHeartbeat() {
  const id = status.value.session_id
  if (!id) return
  try {
    await sendLiveViewHeartbeat(id)
  } catch {
    // Best-effort — the next status poll notices if the session already ended.
  }
}

async function pollStatus() {
  try {
    applyStatus(await getLiveViewStatus())
  } catch {
    // Transient network hiccup — the next poll retries.
  }
}

function startTimers() {
  stopTimers()
  statusTimer = setInterval(pollStatus, STATUS_POLL_INTERVAL_MS)
  heartbeatTimer = setInterval(sendHeartbeat, HEARTBEAT_INTERVAL_MS)
}

function applyStatus(s: LiveViewStatus) {
  const prevSessionId = status.value.session_id
  status.value = s

  if (s.active && s.camera) selectedCamera.value = s.camera

  if (s.active && s.state === 'live' && s.session_id && s.session_id !== prevSessionId) {
    const p = ensurePlayer()
    p.src([{ src: liveViewPlaylistUrl(s.session_id), type: 'application/x-mpegURL' }])
    p.play()?.catch(() => {})
  }

  if (!s.active) {
    stopTimers()
    player?.pause()
    starting.value = false
  }
}

async function loadCameras() {
  loadingCameras.value = true
  try {
    const res = await getLiveViewCameras()
    cameras.value = res.cameras
  } catch {
    toast.show('Failed to load cameras', true)
  } finally {
    loadingCameras.value = false
  }
}

async function selectCamera(camera: string | null) {
  if (!camera) return
  selectedCamera.value = camera
  starting.value = true
  try {
    applyStatus(await startLiveView(camera))
    startTimers()
  } catch (err) {
    toast.show(err instanceof Error ? err.message : 'Failed to start live view', true)
  } finally {
    starting.value = false
  }
}

async function stop() {
  const id = status.value.session_id
  stopTimers()
  status.value = { active: false }
  player?.pause()
  try {
    await stopLiveView(id)
  } catch {
    toast.show('Failed to stop live view', true)
  }
}

onMounted(async () => {
  await loadCameras()
  try {
    // Adopt an already-active session (e.g. started from another browser
    // tab) instead of presenting an empty picker while it's running.
    const s = await getLiveViewStatus()
    if (s.active) {
      applyStatus(s)
      startTimers()
    }
  } catch {
    // Picker is still usable even if this initial check fails.
  }
})

onUnmounted(() => {
  stopTimers()
  if (status.value.session_id) void stopLiveView(status.value.session_id)
  player?.dispose()
})
</script>

<template>
  <div class="liveview-page">
    <h2>Live View</h2>
    <p class="page-intro">
      Watch a live stream from one of your Blink cameras, right in the browser. Only one camera streams at a time —
      picking a different camera switches the stream, and the session stops automatically after a period of inactivity
      or when you navigate away.
    </p>

    <div v-if="loadingCameras" style="padding: 1rem"><LoadingIndicator /></div>
    <Message v-else-if="!cameras.length" severity="info" size="small" :closable="false">
      No cameras found — make sure Blink is connected and your account has at least one camera.
    </Message>
    <div v-else class="camera-picker">
      <SelectButton
        :model-value="selectedCamera"
        :options="cameras"
        :allow-empty="false"
        class="camera-toggle"
        aria-label="Choose a camera"
        @update:model-value="selectCamera"
      />
      <Button v-if="status.active" size="small" severity="secondary" @click="stop">■ Stop</Button>
    </div>

    <Message v-if="status.state === 'error'" severity="error" size="small" :closable="false" class="status-banner">
      {{ status.error || 'Live view stopped unexpectedly.' }}
    </Message>

    <div class="player-area">
      <!--
        Video.js takes over the <video> tag on init, same reasoning as
        ClipModal.vue — never toggle this element with v-if/v-else, only the
        wrapper's class, or a re-render would tear it out from under the
        player instance.
      -->
      <div class="video-js-wrap" :class="{ 'video-hidden': !status.active }">
        <video ref="videoEl" class="video-js vjs-big-play-centered" playsinline>
          <p class="vjs-no-js">JavaScript is required to watch live view.</p>
        </video>
      </div>
      <div v-if="starting || status.state === 'starting'" class="player-placeholder">
        <LoadingIndicator label="Starting live view…" />
      </div>
      <div v-else-if="!status.active" class="player-placeholder muted-note">
        Select a camera above to start watching.
      </div>
    </div>
  </div>
</template>

<style scoped>
.liveview-page {
  padding: 1.75rem;
  padding-bottom: 3rem;
  max-width: 900px;
  /* Flex items default to min-width:auto, refusing to shrink below their
     content's natural width — on a narrow (mobile) viewport that pushed
     this whole page wider than the screen instead of wrapping its text,
     the same fix VehiclesPage/StoragePage already have. */
  min-width: 0;
  width: 100%;
}

.page-intro {
  color: var(--muted);
  margin-bottom: 1rem;
}

.camera-picker {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 1rem;
}

.camera-toggle :deep(.p-selectbutton) {
  flex-wrap: wrap;
  row-gap: 0.4rem;
}

.status-banner {
  margin-bottom: 1rem;
}

.player-area {
  position: relative;
  background: #000;
  border-radius: 8px;
  overflow: hidden;
  min-height: 240px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.player-placeholder {
  padding: 2rem 1rem;
  text-align: center;
  width: 100%;
}

.muted-note {
  color: var(--muted);
  font-size: 0.85rem;
}
</style>
