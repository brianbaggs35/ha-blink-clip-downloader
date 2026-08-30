<script lang="ts">
// Module scope — survives across this component's own mount/unmount cycles,
// unlike anything declared inside <script setup> below (which re-executes
// fresh on every mount, so it can't remember what a previous mount did).
// Lets a fresh mount wait for a stop request the PREVIOUS mount just issued
// to actually land server-side before trusting a status check — see
// onMounted/onUnmounted below for why that ordering matters.
let pendingStop: Promise<unknown> | null = null
</script>

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
const sessionFailureCount = ref(0)

const videoEl = ref<HTMLVideoElement | null>(null)
let player: Player | null = null
// Tracks which session_id the player's source was last set for — NOT the
// same as "session_id changed since last status", since the common case is
// the *same* session transitioning starting -> live with no id change at
// all. Sourcing only on an id change would miss that transition entirely.
let sourcedSessionId: string | null = null
// Set once onUnmounted has run — checked after every await in an
// in-flight async handler so its continuation can't act on a dead
// component instance (see selectCamera/onMounted below).
let unmounted = false
// Bumped by both selectCamera() and stop() so an in-flight
// startLiveView() call can tell, once it resolves, whether it's still the
// most recent request — a later selectCamera() (fast camera-switching) or
// an explicit stop() press must both win over an older, slower one that
// only just now finished starting.
let selectGeneration = 0

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
  player.on('error', () => {
    if (sessionFailureCount.value < 1) {
      toast.show('Live view playback error', true)
      sessionFailureCount.value++
    }
    // If we still believe this session is live, the source may have been
    // set against a manifest that existed but wasn't playable yet — clear
    // the latch so the next status poll (still reporting the same live
    // session) re-attempts sourcing, instead of leaving the player stuck
    // on a failed load until the whole page is refreshed.
    if (status.value.active && status.value.state === 'live') {
      sourcedSessionId = null
    }
  })
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
  status.value = s

  if (s.active && s.camera) selectedCamera.value = s.camera

  if (s.active && s.state === 'live' && s.session_id && s.session_id !== sourcedSessionId) {
    sourcedSessionId = s.session_id
    const sessionId = s.session_id
    const p = ensurePlayer()
    // Video.js's tech (VHS, for HLS) isn't necessarily mounted the instant
    // videojs() returns from ensurePlayer() — calling .src() before
    // player.ready() fires is a known way to get a spurious, immediately-
    // superseded MEDIA_ERR_SRC_NOT_SUPPORTED ("The media could not be
    // loaded...") on the first camera selected after mount, even though the
    // source is fine and playback goes on to start normally moments later.
    // player.ready() runs its callback right away if the player is already
    // ready, so this is always safe, not just on that first call.
    p.ready(() => {
      p.src([{ src: liveViewPlaylistUrl(sessionId), type: 'application/x-mpegURL' }])
      p.play()?.catch(() => {})
    })
  }

  if (!s.active) {
    sourcedSessionId = null
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
  const generation = ++selectGeneration
  sessionFailureCount.value = 0
  // The backend tears down whatever session was previously active as soon
  // as a *different* camera is requested, even if this new start attempt
  // goes on to fail — so a still-running poll/heartbeat timer for the old
  // session can only poll/heartbeat a session that's already gone, or
  // race applyStatus() into flipping `starting` back off mid-switch.
  stopTimers()
  selectedCamera.value = camera
  starting.value = true
  try {
    const s = await startLiveView(camera)
    if (unmounted || generation !== selectGeneration) {
      // Superseded while the start request was in flight — either we
      // navigated away (onUnmounted already ran, with nothing yet to
      // stop), a newer selectCamera() call is now in charge, or stop()
      // was pressed. Either way we're the only thing that knows this
      // particular session exists, so tear it down now rather than
      // leaking a live session with no heartbeats until the backend's
      // idle timeout eventually notices.
      void stopLiveView(s.session_id)
      return
    }
    applyStatus(s)
    startTimers()
  } catch (err) {
    if (!unmounted && generation === selectGeneration) {
      toast.show(err instanceof Error ? err.message : 'Failed to start live view', true)
    }
  } finally {
    if (generation === selectGeneration) starting.value = false
  }
}

async function stop() {
  selectGeneration++ // invalidate any in-flight selectCamera() call
  // If that in-flight call is what set `starting`, its own finally block
  // will see itself superseded and (correctly) leave `starting` alone —
  // so this is the only place left that will ever clear it.
  starting.value = false
  const id = status.value.session_id
  stopTimers()
  status.value = { active: false }
  player?.pause()
  try {
    await stopLiveView(id)
  } catch {
    if (!unmounted) toast.show('Failed to stop live view', true)
  }
}

onMounted(async () => {
  await loadCameras()
  if (unmounted) return
  if (pendingStop) {
    // A previous mount of this same tab (quickly navigating away and back)
    // may still have a stop request in flight from onUnmounted below.
    // Wait for it to actually land before trusting a status check, or this
    // mount can "adopt" a session that's a moment away from being torn
    // down server-side — leaving the camera picker stuck showing it as
    // selected even though it's about to stop (or has already stopped, but
    // this status check raced ahead of that).
    await pendingStop
    pendingStop = null
    if (unmounted) return
  }
  try {
    // Adopt an already-active session (e.g. started from another browser
    // tab) instead of presenting an empty picker while it's running.
    const s = await getLiveViewStatus()
    if (unmounted) return
    if (s.active) {
      applyStatus(s)
      startTimers()
    }
  } catch {
    // Picker is still usable even if this initial check fails.
  }
})

onUnmounted(() => {
  unmounted = true
  stopTimers()
  if (status.value.session_id) {
    pendingStop = stopLiveView(status.value.session_id).catch(() => {})
  }
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
        player instance. Hidden whenever `starting` is true or state isn't
        actually "live" yet — not just "active", and not just the absence
        of `starting`. `status` stays stale (still whatever the *previous*
        camera was reporting) for the whole in-flight duration of a
        selectCamera() call, so keying visibility only off `status` left an
        empty video box — or the old camera's still-playing stream, when
        switching cameras — visible side by side with the "Starting live
        view…" placeholder below.
      -->
      <div class="video-js-wrap" :class="{ 'video-hidden': starting || !(status.active && status.state === 'live') }">
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
  /* Stable box from first render, whether idle, starting, or live — Video.js
     only computes its own fluid/aspect-ratio sizing once ensurePlayer()
     initializes it, so without this the box visibly jumps size the moment
     a camera is selected. Matches SecurityFeedPage.vue's tiles, the same
     fix for the same class of problem. */
  aspect-ratio: 16 / 9;
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
