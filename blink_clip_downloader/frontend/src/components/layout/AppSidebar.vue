<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Tag from 'primevue/tag'
import AppIcon from '../icons/AppIcon.vue'
import type { IconName } from '../icons/paths'
import { useThemeStore } from '../../stores/theme'
import { useToastStore } from '../../stores/toast'
import { useConnectionStore } from '../../stores/connection'
import { useLibraryStore } from '../../stores/library'
import { useRefreshStore } from '../../stores/refresh'
import { useCapabilitiesStore } from '../../stores/capabilities'
import { apiPost } from '../../api/client'
import { getCameras, getStats } from '../../api/clips'
import { listFaces } from '../../api/ai'

export type TabName =
  | 'library'
  | 'liveview'
  | 'securityfeed'
  | 'automations'
  | 'status'
  | 'ai'
  | 'usage'
  | 'models'
  | 'vehicles'
  | 'biometrics'
  | 'storage'

const TABS: { name: TabName; label: string; icon: IconName }[] = [
  { name: 'library', label: 'Library', icon: 'tab-library' },
  { name: 'liveview', label: 'Live View', icon: 'tab-liveview' },
  { name: 'securityfeed', label: 'Security Feed', icon: 'tab-securityfeed' },
  { name: 'automations', label: 'Automations', icon: 'tab-automations' },
  { name: 'status', label: 'Status', icon: 'tab-status' },
  { name: 'ai', label: 'AI', icon: 'tab-ai' },
  { name: 'usage', label: 'AI Usage', icon: 'tab-usage' },
  { name: 'models', label: 'Models', icon: 'tab-models' },
  { name: 'vehicles', label: 'Vehicles', icon: 'tab-vehicles' },
  { name: 'biometrics', label: 'Biometrics', icon: 'tab-biometrics' },
  { name: 'storage', label: 'Storage', icon: 'tab-storage' },
]

const activeTab = defineModel<TabName>({ required: true })
const emit = defineEmits<{ help: []; refresh: [] }>()

const theme = useThemeStore()
const toast = useToastStore()
const connection = useConnectionStore()
const library = useLibraryStore()
const refresh = useRefreshStore()
const capabilities = useCapabilitiesStore()

// Biometrics is the one tab that's entirely unusable when face recognition
// can't run (a Raspberry Pi 4's Cortex-A72 CPU, missing dependencies, ...)
// — hiding it avoids sending someone into a tab where every action would
// just fail. Defaults to visible (null = "not checked yet") rather than
// flashing hidden-then-shown once the check completes.
const visibleTabs = computed(() =>
  capabilities.faceRecognitionAvailable === false ? TABS.filter((t) => t.name !== 'biometrics') : TABS,
)

async function pollFaceRecognitionAvailable() {
  try {
    const r = await listFaces()
    capabilities.setFaceRecognitionAvailable(r.available)
  } catch {
    // Transient — leave the tab visible rather than hide it on a single
    // dropped request; the tab's own load() will surface a real error.
  }
}

// Narrow race: the check above resolves after the user has already
// clicked into Biometrics (slow network, or clicked before the initial
// fetch settled). Without this, the tab would vanish from the nav but the
// page would stay stuck showing it, with no visible way back.
watch(
  () => capabilities.faceRecognitionAvailable,
  (available) => {
    if (available === false && activeTab.value === 'biometrics') {
      activeTab.value = 'library'
    }
  },
)

const notifSupported = 'Notification' in window
const notifEnabled = ref(localStorage.getItem('blink_notif') === '1')

async function toggleNotifications() {
  if (!notifEnabled.value) {
    const perm = await Notification.requestPermission()
    if (perm === 'granted') {
      notifEnabled.value = true
      localStorage.setItem('blink_notif', '1')
      toast.show('Browser notifications enabled 🔔')
    } else {
      toast.show('Notification permission denied', true)
    }
  } else {
    notifEnabled.value = false
    localStorage.removeItem('blink_notif')
    toast.show('Notifications disabled')
  }
}

const showAbout = ref(false)
const REPO_URL = 'https://github.com/brianbaggs35/ha-blink-clip-downloader'
const BLINKPY_URL = 'https://github.com/fronzbot/blinkpy'

const connSeverity = computed(() => {
  if (connection.connected === null) return 'secondary'
  return connection.connected ? 'success' : 'danger'
})
const connLabel = computed(() => {
  if (connection.connected === null) return 'Unknown'
  return connection.connected ? 'Connected' : 'Disconnected'
})
const connIcon = computed(() => {
  if (connection.connected === null) return 'pi pi-question-circle'
  return connection.connected ? 'pi pi-wifi' : 'pi pi-times-circle'
})

const cameraTotal = computed(() => library.cameras.reduce((sum, c) => sum + (c.total || 0), 0))

// The camera list is a Library filter shortcut, but it's shown in the
// persistent sidebar (like Sync/Refresh/the connection badge) rather than
// gated to the Library tab, so clicking one from elsewhere must switch
// there too — otherwise the click has no visible effect at all.
function selectCamera(camera: string) {
  library.selectCamera(camera)
  activeTab.value = 'library'
}

const syncing = ref(false)
async function sync() {
  syncing.value = true
  try {
    await apiPost('/api/download-now')
    toast.show('Download triggered — clips appear shortly')
    setTimeout(() => refresh.bump(), 10000)
  } catch {
    toast.show('Sync failed', true)
  } finally {
    setTimeout(() => {
      syncing.value = false
    }, 3000)
  }
}

function onRefreshClick() {
  refresh.bump()
  emit('refresh')
}

// The Connected/Disconnected badge previously only updated as a side effect
// of Library's or Status's own /api/stats polling — if neither tab had ever
// been mounted yet (e.g. the app opened straight to AI while Blink auth was
// still connecting), the badge stayed stuck at whatever it started as until
// the user happened to visit one of those two tabs, or reloaded the page.
// AppSidebar is the one thing that's always mounted for the app's whole
// lifetime, so it's the right place to own this independently.
const CONNECTION_POLL_INTERVAL_MS = 10000
let connectionPollTimer: ReturnType<typeof setInterval> | undefined
let cameraPollTimer: ReturnType<typeof setInterval> | undefined
let cameraSignature = ''
let cameraListInitialized = false
let cameraPollSeq = 0

async function pollConnection() {
  try {
    const s = await getStats()
    if (typeof s.connected === 'boolean') connection.setConnected(s.connected)
  } catch {
    // Transient — leave the badge at its last known state rather than
    // flipping it to Unknown on a single dropped poll.
  }
}

async function pollCameras() {
  const seq = ++cameraPollSeq
  try {
    const cameras = await getCameras()
    if (seq !== cameraPollSeq) return
    const previousCameras = library.cameras
    const signature = cameras
      .map((camera) => camera.camera.toLowerCase())
      .sort()
      .join('\u0001')
    const changed = cameraListInitialized && signature !== cameraSignature
    if (library.currentCamera !== 'all' && !cameras.some((camera) => camera.camera === library.currentCamera)) {
      const additions = cameras.filter(
        (camera) => !previousCameras.some((previous) => previous.camera === camera.camera),
      )
      library.selectCamera(additions.length === 1 ? additions[0].camera : 'all')
    }
    library.setCameras(cameras)
    cameraSignature = signature
    cameraListInitialized = true
    if (changed) refresh.bump()
  } catch {
    // Transient — leave the camera navigation at its last known state.
  }
}

onMounted(() => {
  void pollConnection()
  void pollCameras()
  void pollFaceRecognitionAvailable()
  connectionPollTimer = setInterval(pollConnection, CONNECTION_POLL_INTERVAL_MS)
  cameraPollTimer = setInterval(pollCameras, CONNECTION_POLL_INTERVAL_MS)
})
onUnmounted(() => {
  if (connectionPollTimer) clearInterval(connectionPollTimer)
  if (cameraPollTimer) clearInterval(cameraPollTimer)
})
</script>

<template>
  <nav class="app-nav">
    <div class="app-nav-brand">
      <span class="app-nav-brand-mark"><AppIcon name="brand" /></span>
      <span class="app-nav-brand-text">Blink <strong>Clips</strong></span>
    </div>

    <div class="app-nav-tabs">
      <button
        v-for="tab in visibleTabs"
        :key="tab.name"
        type="button"
        class="app-nav-tab"
        :class="{ active: activeTab === tab.name }"
        :data-tab="tab.name"
        @click="activeTab = tab.name"
      >
        <AppIcon :name="tab.icon" />
        <span>{{ tab.label }}</span>
      </button>
    </div>

    <template v-if="library.cameras.length">
      <div class="app-nav-section-label">Cameras</div>
      <div class="app-nav-cameras">
        <button
          type="button"
          class="app-nav-cam"
          :class="{ active: activeTab === 'library' && library.currentCamera === 'all' }"
          data-camera="all"
          @click="selectCamera('all')"
        >
          <span>All Cameras</span>
          <span class="app-nav-cam-count">{{ cameraTotal }}</span>
        </button>
        <button
          v-for="cam in library.cameras"
          :key="cam.camera"
          type="button"
          class="app-nav-cam"
          :class="{ active: activeTab === 'library' && library.currentCamera === cam.camera }"
          :data-camera="cam.camera"
          @click="selectCamera(cam.camera)"
        >
          <span>{{ cam.camera }}</span>
          <span class="app-nav-cam-count">{{ cam.total || 0 }}</span>
        </button>
      </div>
    </template>

    <div class="app-nav-spacer" />

    <div class="app-nav-utility">
      <Tag :severity="connSeverity" :value="connLabel" :icon="connIcon" rounded class="app-nav-conn-tag" />
      <div class="app-nav-icon-row">
        <Button
          text
          rounded
          size="small"
          severity="secondary"
          title="Toggle dark/light theme"
          :aria-label="theme.isDark ? 'Switch to light theme' : 'Switch to dark theme'"
          @click="theme.toggle()"
        >
          <template #icon><AppIcon :name="theme.isDark ? 'theme-dark' : 'theme-light'" /></template>
        </Button>
        <Button
          text
          rounded
          size="small"
          severity="secondary"
          title="Keyboard shortcuts (?)"
          aria-label="Keyboard shortcuts"
          @click="emit('help')"
        >
          <template #icon><AppIcon name="help" /></template>
        </Button>
        <Button
          v-if="notifSupported"
          text
          rounded
          size="small"
          severity="secondary"
          :title="notifEnabled ? 'Notifications ON (click to disable)' : 'Enable browser notifications'"
          :aria-label="notifEnabled ? 'Notifications on' : 'Enable browser notifications'"
          @click="toggleNotifications"
        >
          <template #icon><AppIcon :name="notifEnabled ? 'notif-on' : 'notif-off'" /></template>
        </Button>
        <Button
          text
          rounded
          size="small"
          severity="secondary"
          icon="pi pi-info-circle"
          title="About this app"
          aria-label="About this app"
          @click="showAbout = true"
        />
      </div>
      <div class="app-nav-action-row">
        <Button size="small" severity="secondary" outlined label="Refresh" title="Refresh" @click="onRefreshClick">
          <template #icon><AppIcon name="refresh" /></template>
        </Button>
        <Button size="small" :label="syncing ? 'Syncing…' : 'Sync'" title="Sync" :disabled="syncing" @click="sync">
          <template #icon><AppIcon name="sync" /></template>
        </Button>
      </div>
    </div>

    <Dialog
      v-model:visible="showAbout"
      modal
      dismissable-mask
      header="About Blink Clips 5.4.8"
      :style="{ width: '26rem' }"
      :draggable="false"
    >
      <p class="m-0">Built by Brian Baggs.</p>
      <p>
        <a :href="REPO_URL" target="_blank" rel="noopener">{{ REPO_URL }}</a>
      </p>
      <p class="m-0">
        Built on <a :href="BLINKPY_URL" target="_blank" rel="noopener">blinkpy</a>, an open-source Blink camera API
        client.
      </p>
    </Dialog>
  </nav>
</template>
