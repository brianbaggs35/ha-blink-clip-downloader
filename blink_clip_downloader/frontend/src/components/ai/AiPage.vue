<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { getAiStatus } from '../../api/ai'
import type { AiStatus } from '../../api/types'
import AdaptiveLearningCard from './AdaptiveLearningCard.vue'
import AiConnectionCard from './AiConnectionCard.vue'
import AiStatusCards from './AiStatusCards.vue'
import CameraConfigsSection from './CameraConfigsSection.vue'
import EmailAlertsCard from './EmailAlertsCard.vue'
import FaceRecognitionSection from './FaceRecognitionSection.vue'
import FineTuneCard from './FineTuneCard.vue'
import SuspiciousFeed from './SuspiciousFeed.vue'

const status = ref<AiStatus | null>(null)
const loading = ref(true)

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
</script>

<template>
  <div class="auto-content">
    <h2>AI Video Analysis</h2>
    <div v-if="loading" style="padding: 2rem; text-align: center; color: var(--muted)">Loading…</div>
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
        <AdaptiveLearningCard />
        <FineTuneCard v-if="status.provider === 'moondream_cloud'" @activated="load" />
      </div>

      <CameraConfigsSection :car-protection-active="status.car_protection_active ?? null" />
      <FaceRecognitionSection />
      <SuspiciousFeed />
    </template>
  </div>
</template>
