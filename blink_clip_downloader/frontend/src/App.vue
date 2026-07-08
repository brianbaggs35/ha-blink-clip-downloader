<script setup lang="ts">
import { onMounted, onUnmounted, ref, watchEffect } from 'vue'
import AppSidebar, { type TabName } from './components/layout/AppSidebar.vue'
import ToastHost from './components/layout/ToastHost.vue'
import ConfirmDialog from './components/layout/ConfirmDialog.vue'
import HelpOverlay from './components/layout/HelpOverlay.vue'
import TwoFAOverlay from './components/layout/TwoFAOverlay.vue'
import AuthErrorBanner from './components/layout/AuthErrorBanner.vue'
import AutomationsPage from './components/automations/AutomationsPage.vue'
import { useThemeStore } from './stores/theme'
import { useAuthStore } from './stores/auth'
import { useKeyboardShortcuts } from './composables/useKeyboardShortcuts'

const activeTab = ref<TabName>('library')
const helpOpen = ref(false)

const theme = useThemeStore()
const auth = useAuthStore()

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
    <!-- Ported in a follow-up phase (Library tab). -->
  </div>
  <div id="page-automations" class="page" :class="{ active: activeTab === 'automations' }">
    <AutomationsPage />
  </div>
  <div id="page-status" class="page" :class="{ active: activeTab === 'status' }">
    <!-- Ported in a follow-up phase (Status tab). -->
  </div>
  <div id="page-ai" class="page" :class="{ active: activeTab === 'ai' }">
    <!-- Ported in a follow-up phase (AI config tab). -->
  </div>
  <div id="page-usage" class="page" :class="{ active: activeTab === 'usage' }">
    <!-- Ported in a follow-up phase (AI Usage tab). -->
  </div>
  <div id="page-models" class="page" :class="{ active: activeTab === 'models' }">
    <!-- New tab (task #12): provider/model reference, pricing, docs links. -->
  </div>

  <HelpOverlay v-model="helpOpen" />
  <AuthErrorBanner />
  <TwoFAOverlay />
  <ConfirmDialog />
  <ToastHost />
</template>
