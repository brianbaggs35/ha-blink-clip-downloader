<script setup lang="ts">
import { ref, watch } from 'vue'
import { analyzeClipNow, getClipAiResult, getFeedbackForClip, submitFeedback } from '../../api/ai'
import type { AnalysisResultDict, Feedback } from '../../api/types'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ clipId: string; promptDebugEnabled?: boolean }>()

const toast = useToastStore()
const promptOverlay = usePromptOverlayStore()

const expanded = ref(false)
const loading = ref(false)
const loadError = ref(false)
const result = ref<AnalysisResultDict | null>(null)
const loaded = ref(false)
const feedback = ref<Feedback | null>(null)
const showRawResponse = ref(false)
const showFeedbackForm = ref(false)
const feedbackNote = ref('')
const feedbackCorrectedSuspicious = ref(false)
const analyzing = ref(false)

function reset() {
  expanded.value = false
  loaded.value = false
  result.value = null
  loadError.value = false
  feedback.value = null
  showRawResponse.value = false
  showFeedbackForm.value = false
  feedbackNote.value = ''
  feedbackCorrectedSuspicious.value = false
}
watch(() => props.clipId, reset)

async function load() {
  loading.value = true
  loadError.value = false
  try {
    result.value = await getClipAiResult(props.clipId)
    feedback.value = result.value ? await getFeedbackForClip(props.clipId).catch(() => null) : null
    loaded.value = true
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

function toggle() {
  expanded.value = !expanded.value
  if (expanded.value && !loaded.value) void load()
}

async function analyzeNow() {
  analyzing.value = true
  try {
    await analyzeClipNow(props.clipId)
    loaded.value = false
    await load()
    toast.show('AI analysis complete')
  } catch {
    loadError.value = true
    toast.show('Analysis failed', true)
  } finally {
    analyzing.value = false
  }
}

async function quickFeedback(correct: boolean) {
  const ok = await trySubmitFeedback({ correct })
  if (ok) await load()
}

function openFeedbackNoteForm() {
  showFeedbackForm.value = true
}

async function submitFeedbackFormClick() {
  const ok = await trySubmitFeedback({
    correct: false,
    correction_note: feedbackNote.value,
    corrected_suspicious: feedbackCorrectedSuspicious.value ? true : undefined,
  })
  if (ok) {
    showFeedbackForm.value = false
    feedbackNote.value = ''
    feedbackCorrectedSuspicious.value = false
    await load()
  }
}

async function trySubmitFeedback(body: {
  correct: boolean
  correction_note?: string
  corrected_suspicious?: boolean
}): Promise<boolean> {
  try {
    await submitFeedback(props.clipId, body)
    toast.show('Feedback recorded — thanks!')
    return true
  } catch {
    toast.show('Failed to save feedback', true)
    return false
  }
}

function changeFeedback() {
  feedback.value = null
}

function showPrompt() {
  promptOverlay.show(result.value?.prompt_text || '')
}

const confPct = (r: AnalysisResultDict) => Math.round((r.confidence || 0) * 100)
</script>

<template>
  <div class="ai-panel">
    <button class="ai-panel-hdr" :class="{ open: expanded }" @click="toggle">
      🤖 <strong style="font-weight: 600">AI Analysis</strong>
      <span
        v-if="result"
        style="font-size: 0.8rem"
        :style="{ color: result.is_suspicious ? 'var(--danger)' : 'var(--success)' }"
      >
        {{ result.is_suspicious ? ' ⚠' : ' ✓' }}
      </span>
      <span class="chevron">▶</span>
    </button>
    <div class="ai-panel-body" :class="{ open: expanded }">
      <div style="padding-bottom: 0.3rem">
        <span v-if="loading" style="color: var(--muted); font-size: 0.8rem">Loading…</span>
        <span v-else-if="loadError" style="color: var(--danger); font-size: 0.8rem">Failed to load analysis</span>
        <template v-else-if="loaded && !result">
          <div style="color: var(--muted); font-size: 0.8rem; margin-bottom: 0.45rem">Not analyzed yet</div>
          <button class="btn sm" :disabled="analyzing" @click="analyzeNow">
            {{ analyzing ? '⏳ Analyzing…' : '🔬 Analyze Now' }}
          </button>
        </template>
        <div v-else-if="result" class="ai-result-box">
          <div style="display: flex; align-items: center; gap: 0.55rem; margin-bottom: 0.4rem">
            <span v-if="result.is_suspicious" class="ai-badge-suspicious">⚠ Suspicious</span>
            <span v-else class="ai-badge-clean">✓ Clear</span>
            <span style="font-weight: 600" :style="{ color: result.is_suspicious ? 'var(--danger)' : 'var(--success)' }"
              >{{ confPct(result) }}% confidence</span
            >
          </div>
          <div v-if="result.summary" style="color: var(--text); line-height: 1.45; margin-bottom: 0.4rem">
            {{ result.summary }}
          </div>
          <div style="color: var(--muted); font-size: 0.74rem; margin-bottom: 0.4rem">
            Model: {{ result.model || '—' }}
            <template v-if="result.analyzed_at">
              &nbsp;·&nbsp; {{ new Date(result.analyzed_at).toLocaleString() }}</template
            >
            <template v-if="result.frame_count"> &nbsp;·&nbsp; {{ result.frame_count }} frame(s) analyzed</template>
          </div>
          <div style="display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap">
            <button class="btn sm ghost" :disabled="analyzing" @click="analyzeNow">↺ Re-analyze</button>
            <button class="btn sm ghost" @click="showRawResponse = !showRawResponse">
              {{ showRawResponse ? '📄 Hide response' : '📄 Full response' }}
            </button>
            <button v-if="promptDebugEnabled" class="btn sm ghost" @click="showPrompt">📝 Prompt</button>
          </div>
          <div
            v-if="showRawResponse"
            style="
              margin-top: 0.4rem;
              font-size: 0.73rem;
              font-family: monospace;
              background: var(--card2);
              border-radius: 4px;
              padding: 0.4rem 0.5rem;
              white-space: pre-wrap;
              color: var(--muted);
              max-height: 120px;
              overflow-y: auto;
            "
          >
            {{ result.response_text || '' }}
          </div>
          <div style="margin-top: 0.55rem; padding-top: 0.5rem; border-top: 1px solid var(--border)">
            <div
              v-if="feedback"
              style="font-size: 0.78rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap"
            >
              <span v-if="feedback.correct" style="color: var(--success)">👍 Marked correct</span>
              <span v-else style="color: var(--warn)"
                >👎 Marked incorrect<template v-if="feedback.correction_note">
                  — "{{ feedback.correction_note }}"</template
                ></span
              >
              <button class="btn sm ghost" @click="changeFeedback">Change</button>
            </div>
            <div v-else style="font-size: 0.78rem; display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap">
              <span style="color: var(--muted)">Was this verdict correct?</span>
              <button class="btn sm ghost" @click="quickFeedback(true)">👍 Correct</button>
              <button class="btn sm ghost" @click="openFeedbackNoteForm">👎 Incorrect</button>
            </div>
            <div
              v-if="showFeedbackForm"
              style="margin-top: 0.4rem; display: flex; flex-direction: column; gap: 0.35rem"
            >
              <input
                v-model="feedbackNote"
                class="tag-input"
                style="width: 100%"
                placeholder="What actually happened? (optional)"
              />
              <label style="font-size: 0.75rem; color: var(--muted); display: flex; align-items: center; gap: 0.3rem">
                <input v-model="feedbackCorrectedSuspicious" type="checkbox" /> Should have been flagged suspicious
                instead
              </label>
              <div style="display: flex; gap: 0.4rem">
                <button class="btn sm" @click="submitFeedbackFormClick">Submit</button>
                <button class="btn sm ghost" @click="showFeedbackForm = false">Cancel</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
