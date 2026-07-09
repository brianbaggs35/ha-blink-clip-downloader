<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import AppSidebar, { type TabName } from './components/layout/AppSidebar.vue'
import ToastHost from './components/layout/ToastHost.vue'
import ConfirmDialog from './components/layout/ConfirmDialog.vue'
import HelpOverlay from './components/layout/HelpOverlay.vue'
import TwoFAOverlay from './components/layout/TwoFAOverlay.vue'
import AuthErrorBanner from './components/layout/AuthErrorBanner.vue'
import AutomationsPage from './components/automations/AutomationsPage.vue'
import StatusPage from './components/status/StatusPage.vue'
import LibraryPage from './components/library/LibraryPage.vue'
import AiPage from './components/ai/AiPage.vue'
import UsagePage from './components/usage/UsagePage.vue'
import PromptOverlay from './components/layout/PromptOverlay.vue'
import { useThemeStore } from './stores/theme'
import { useAuthStore } from './stores/auth'
import { useDateFilterStore } from './stores/dateFilter'
import { useKeyboardShortcuts } from './composables/useKeyboardShortcuts'

const activeTab = ref<TabName>('library')
const helpOpen = ref(false)

const theme = useThemeStore()
const auth = useAuthStore()
const dateFilter = useDateFilterStore()

// The Status tab's activity-chart drill-down requests a switch to Library
// filtered to a specific day — see stores/dateFilter.ts.
watch(
  () => dateFilter.seq,
  (seq) => {
    if (seq > 0) activeTab.value = 'library'
  },
)

// Theme is applied to <body>, not #app — the whole viewport (including
// fixed-position overlays that render outside #app's flex layout) needs the
// CSS custom properties the .dark/.light classes provide.
watchEffect(() => {
  document.body.classList.toggle('dark', theme.isDark)
  document.body.classList.toggle('light', !theme.isDark)
})

useKeyboardShortcuts(helpOpen)

onMounted(() => auth.startPolling())
onUnmounted(() => auth.stopPolling())
</script>

<template>
  <AppSidebar v-model="activeTab" @help="helpOpen = !helpOpen" />

  <div id="page-library" class="page" :class="{ active: activeTab === 'library' }">
    <LibraryPage />
  </div>
  <div id="page-automations" class="page" :class="{ active: activeTab === 'automations' }">
    <AutomationsPage />
  </div>
  <div id="page-status" class="page" :class="{ active: activeTab === 'status' }">
    <StatusPage v-if="activeTab === 'status'" />
  </div>
  <div id="page-ai" class="page" :class="{ active: activeTab === 'ai' }">
    <AiPage v-if="activeTab === 'ai'" />
  </div>
  <div id="page-usage" class="page" :class="{ active: activeTab === 'usage' }">
    <UsagePage v-if="activeTab === 'usage'" />
  </div>
  <div id="page-models" class="page" :class="{ active: activeTab === 'models' }">
    <!-- New tab (task #12): provider/model reference, pricing, docs links. -->
  </div>

  <HelpOverlay v-model="helpOpen" />
  <PromptOverlay />
  <AuthErrorBanner />
  <TwoFAOverlay />
  <ConfirmDialog />
  <ToastHost />
</template>
