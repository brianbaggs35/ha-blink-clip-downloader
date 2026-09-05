<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { getAiStatus } from '../../api/ai'
import type { AiStatus } from '../../api/types'
import { useRefreshStore } from '../../stores/refresh'
import LoadingIndicator from '../layout/LoadingIndicator.vue'
import AdaptiveLearningCard from './AdaptiveLearningCard.vue'
import AiAnalysisConfigCard from './AiAnalysisConfigCard.vue'
import AiConnectionCard from './AiConnectionCard.vue'
import AiStatusCards from './AiStatusCards.vue'
import CameraConfigsSection from './CameraConfigsSection.vue'
import EmailAlertsCard from './EmailAlertsCard.vue'
import FineTuneCard from './FineTuneCard.vue'
import SuspiciousFeed from './SuspiciousFeed.vue'

const status = ref<AiStatus | null>(null)
const loading = ref(true)
const refresh = useRefreshStore()
const adaptiveLearningCard = ref<InstanceType<typeof AdaptiveLearningCard> | null>(null)
const suspiciousFeed = ref<InstanceType<typeof SuspiciousFeed> | null>(null)

async function load() {
  try {
    status.value = await getAiStatus()
  } finally {
    loading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval>
onMounted(() => {
  void load()
  // Mirrors the pre-Vue UI's 10s auto-refresh while the AI tab is active —
  // here that's simply "while this component is mounted" (see App.vue,
  // which mounts this tab with v-if).
  pollTimer = setInterval(load, 10_000)
})
onUnmounted(() => clearInterval(pollTimer))

// AdaptiveLearningCard/SuspiciousFeed each expose reload() for exactly this:
// feedback submitted from a clip's AI panel (reachable from this tab's
// Suspicious Activity Feed without switching tabs — see ClipAiPanel.vue)
// bumps this same shared refresh signal, since neither child otherwise has
// any way to learn that feedback changed underneath it.
watch(
  () => refresh.tick,
  () => {
    void adaptiveLearningCard.value?.reload()
    void suspiciousFeed.value?.reload()
  },
)
</script>

<template>
  <div class="auto-content">
    <h2>AI Video Analysis</h2>
    <div v-if="loading" style="padding: 2rem"><LoadingIndicator /></div>
    <div v-else-if="!status?.enabled" class="card" style="padding: 2rem; text-align: center; color: var(--muted)">
      <p style="font-size: 1.2rem; margin-bottom: 0.8rem">🤖 AI Analysis Not Configured</p>
      <p>
        Enable AI analysis in the add-on settings and select a provider (Ollama, Moondream Cloud, or Moondream Local).
      </p>
    </div>
    <template v-else>
      <div
        style="
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(min(280px, 100%), 1fr));
          gap: 1rem;
          margin-bottom: 1.5rem;
        "
      >
        <AiConnectionCard :status="status" />
        <AiStatusCards :status="status" />
        <EmailAlertsCard :smtp-configured="status.smtp_configured" />
        <AdaptiveLearningCard ref="adaptiveLearningCard" />
        <FineTuneCard v-if="status.provider === 'moondream_cloud'" @activated="load" />
      </div>

      <AiAnalysisConfigCard />
      <CameraConfigsSection />
      <SuspiciousFeed ref="suspiciousFeed" />
    </template>
  </div>
</template>
