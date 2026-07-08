<script setup lang="ts">
import { computed, ref } from 'vue'
import AppIcon from '../icons/AppIcon.vue'
import { useThemeStore } from '../../stores/theme'
import { useToastStore } from '../../stores/toast'
import { useConnectionStore } from '../../stores/connection'
import { useRefreshStore } from '../../stores/refresh'
import { apiPost } from '../../api/client'

export type TabName = 'library' | 'automations' | 'status' | 'ai' | 'usage' | 'models'

const TABS: { name: TabName; label: string; icon: string }[] = [
  { name: 'library', label: 'Library', icon: 'tab-library' },
  { name: 'automations', label: 'Automations', icon: 'tab-automations' },
  { name: 'status', label: 'Status', icon: 'tab-status' },
  { name: 'ai', label: 'AI', icon: 'tab-ai' },
  { name: 'usage', label: 'AI Usage', icon: 'tab-usage' },
  { name: 'models', label: 'Models', icon: 'tab-models' },
]

const activeTab = defineModel<TabName>({ required: true })
const emit = defineEmits<{ help: []; refresh: [] }>()

const theme = useThemeStore()
const toast = useToastStore()
const connection = useConnectionStore()
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

const connBadgeClass = computed(() => {
  if (connection.connected === null) return ''
  return connection.connected ? 'ok' : 'err'
})
const connBadgeText = computed(() => {
  if (connection.connected === null) return '●'
  return connection.connected ? '● Connected' : '● Disconnected'
})

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
  <nav class="nav">
    <div class="nav-brand">
      <span class="brand-mark"><AppIcon name="brand" /></span>
      Blink <span>Clips</span>
    </div>
    <div class="nav-tabs">
      <button
        v-for="tab in TABS"
        :key="tab.name"
        class="nav-tab"
        :class="{ active: activeTab === tab.name }"
        :data-tab="tab.name"
        @click="activeTab = tab.name"
      >
        <AppIcon :name="tab.icon" /> <span>{{ tab.label }}</span>
      </button>
    </div>
    <div class="nav-actions">
      <button
        class="btn icon ghost"
        title="Toggle dark/light theme"
        :aria-label="theme.isDark ? 'Switch to light theme' : 'Switch to dark theme'"
        @click="theme.toggle()"
      >
        <AppIcon :name="theme.isDark ? 'theme-dark' : 'theme-light'" />
      </button>
      <button class="btn icon ghost" title="Keyboard shortcuts (?)" @click="emit('help')">
        <AppIcon name="help" />
      </button>
      <button
        v-if="notifSupported"
        class="btn icon ghost"
        :title="
          notifEnabled ? 'Notifications ON (click to disable)' : 'Enable browser notifications'
        "
        @click="toggleNotifications"
      >
        <AppIcon :name="notifEnabled ? 'notif-on' : 'notif-off'" />
      </button>
      <span class="badge" :class="connBadgeClass">{{ connBadgeText }}</span>
      <button class="btn sm outline" @click="onRefreshClick">
        <AppIcon name="refresh" /> Refresh
      </button>
      <button class="btn sm" :disabled="syncing" @click="sync">
        <AppIcon name="sync" /> {{ syncing ? 'Syncing…' : 'Sync' }}
      </button>
    </div>
  </nav>
</template>
