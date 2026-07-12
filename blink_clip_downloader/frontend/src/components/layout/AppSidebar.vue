<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Tag from 'primevue/tag'
import AppIcon from '../icons/AppIcon.vue'
import { useThemeStore } from '../../stores/theme'
import { useToastStore } from '../../stores/toast'
import { useConnectionStore } from '../../stores/connection'
import { useLibraryStore } from '../../stores/library'
import { useRefreshStore } from '../../stores/refresh'
import { apiPost } from '../../api/client'

export type TabName = 'library' | 'automations' | 'status' | 'ai' | 'usage' | 'models' | 'vehicles' | 'biometrics'

const TABS: { name: TabName; label: string; icon: string }[] = [
  { name: 'library', label: 'Library', icon: 'tab-library' },
  { name: 'automations', label: 'Automations', icon: 'tab-automations' },
  { name: 'status', label: 'Status', icon: 'tab-status' },
  { name: 'ai', label: 'AI', icon: 'tab-ai' },
  { name: 'usage', label: 'AI Usage', icon: 'tab-usage' },
  { name: 'models', label: 'Models', icon: 'tab-models' },
  { name: 'vehicles', label: 'Vehicles', icon: 'tab-vehicles' },
  { name: 'biometrics', label: 'Biometrics', icon: 'tab-biometrics' },
]

const activeTab = defineModel<TabName>({ required: true })
const emit = defineEmits<{ help: []; refresh: [] }>()

const theme = useThemeStore()
const toast = useToastStore()
const connection = useConnectionStore()
const library = useLibraryStore()
const refresh = useRefreshStore()

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

const connSeverity = computed(() => {
  if (connection.connected === null) return 'secondary'
  return connection.connected ? 'success' : 'danger'
})
const connLabel = computed(() => {
  if (connection.connected === null) return 'Unknown'
  return connection.connected ? 'Connected' : 'Disconnected'
})

const cameraTotal = computed(() => library.cameras.reduce((sum, c) => sum + (c.total || 0), 0))

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
</script>

<template>
  <nav class="app-nav">
    <div class="app-nav-brand">
      <span class="app-nav-brand-mark"><AppIcon name="brand" /></span>
      <span class="app-nav-brand-text">Blink <strong>Clips</strong></span>
    </div>

    <div class="app-nav-tabs">
      <button
        v-for="tab in TABS"
        :key="tab.name"
        class="app-nav-tab"
        :class="{ active: activeTab === tab.name }"
        :data-tab="tab.name"
        @click="activeTab = tab.name"
      >
        <AppIcon :name="tab.icon" />
        <span>{{ tab.label }}</span>
      </button>
    </div>

    <template v-if="activeTab === 'library'">
      <div class="app-nav-section-label">Cameras</div>
      <div class="app-nav-cameras">
        <button
          class="app-nav-cam"
          :class="{ active: library.currentCamera === 'all' }"
          data-camera="all"
          @click="library.selectCamera('all')"
        >
          <span>All Cameras</span>
          <span class="app-nav-cam-count">{{ cameraTotal }}</span>
        </button>
        <button
          v-for="cam in library.cameras"
          :key="cam.camera"
          class="app-nav-cam"
          :class="{ active: library.currentCamera === cam.camera }"
          :data-camera="cam.camera"
          @click="library.selectCamera(cam.camera)"
        >
          <span>{{ cam.camera }}</span>
          <span class="app-nav-cam-count">{{ cam.total || 0 }}</span>
        </button>
      </div>
    </template>

    <div class="app-nav-spacer" />

    <div class="app-nav-utility">
      <Tag :severity="connSeverity" :value="connLabel" class="app-nav-conn-tag" />
      <div class="app-nav-icon-row">
        <Button
          text
          rounded
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
          severity="secondary"
          :title="notifEnabled ? 'Notifications ON (click to disable)' : 'Enable browser notifications'"
          :aria-label="notifEnabled ? 'Notifications on' : 'Enable browser notifications'"
          @click="toggleNotifications"
        >
          <template #icon><AppIcon :name="notifEnabled ? 'notif-on' : 'notif-off'" /></template>
        </Button>
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
  </nav>
</template>
