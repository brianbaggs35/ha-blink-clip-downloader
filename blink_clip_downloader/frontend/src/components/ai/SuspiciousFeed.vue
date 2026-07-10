<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getSuspiciousClips, submitFeedback } from '../../api/ai'
import type { SuspiciousClip } from '../../api/types'
import { useClipViewerStore } from '../../stores/clipViewer'

const clipViewer = useClipViewerStore()

const items = ref<SuspiciousClip[]>([])
const loading = ref(true)
const loadError = ref(false)
const thanked = ref<Set<string>>(new Set())

async function load() {
  loading.value = true
  loadError.value = false
  try {
    items.value = await getSuspiciousClips(20)
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)
defineExpose({ reload: load })

function confPct(item: SuspiciousClip): number {
  return Math.round((item.confidence || 0) * 100)
}

function openClip(clipId: string) {
  clipViewer.requestOpen(clipId)
}

async function quickFeedback(clipId: string, correct: boolean) {
  try {
    await submitFeedback(clipId, { correct })
    thanked.value = new Set(thanked.value).add(clipId)
  } catch {
    /* leave the buttons in place so the reviewer can retry */
  }
}
</script>

<template>
  <div>
    <h3 style="margin-bottom: 0.8rem">Suspicious Activity Feed</h3>
    <div style="display: flex; flex-direction: column; gap: 0.5rem">
      <div v-if="loading" style="color: var(--muted); padding: 1rem; text-align: center">Loading…</div>
      <div v-else-if="loadError" style="color: var(--danger); padding: 1rem; text-align: center">
        Failed to load suspicious activity.
      </div>
      <div v-else-if="!items.length" style="color: var(--muted); padding: 1rem; text-align: center">
        No suspicious activity detected yet.
      </div>
      <template v-else>
        <div
          v-for="item in items"
          :key="item.clip_id"
          class="card"
          style="padding: 0.8rem; display: flex; align-items: center; gap: 1rem; cursor: pointer"
          @click="openClip(item.clip_id)"
        >
          <div style="font-size: 1.3rem">⚠️</div>
          <div style="flex: 1; min-width: 0">
            <div style="font-weight: 600; font-size: 0.85rem">{{ item.camera }}</div>
            <div style="font-size: 0.78rem; color: var(--muted)">{{ new Date(item.analyzed_at).toLocaleString() }}</div>
            <div style="font-size: 0.82rem; margin-top: 0.3rem">{{ item.summary || '' }}</div>
          </div>
          <div style="text-align: center; min-width: 50px">
            <div
              style="font-size: 1.1rem; font-weight: 700"
              :style="{ color: confPct(item) > 70 ? 'var(--danger)' : 'var(--warn)' }"
            >
              {{ confPct(item) }}%
            </div>
            <div style="font-size: 0.65rem; color: var(--muted)">confidence</div>
          </div>
          <div style="display: flex; gap: 0.25rem" @click.stop>
            <span v-if="thanked.has(item.clip_id)" style="font-size: 0.72rem; color: var(--muted)">Thanks!</span>
            <template v-else>
              <button class="btn sm ghost" title="Correct" @click="quickFeedback(item.clip_id, true)">👍</button>
              <button class="btn sm ghost" title="Incorrect" @click="quickFeedback(item.clip_id, false)">👎</button>
            </template>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>
