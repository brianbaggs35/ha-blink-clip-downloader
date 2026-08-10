<script setup lang="ts">
import { onUnmounted, ref, watch } from 'vue'
import {
  fetchAiModels,
  fetchEscalationModels,
  getMoondreamInstallStatus,
  startMoondreamInstall,
  analyzeClipNow,
} from '../../api/ai'
import { listClips } from '../../api/clips'
import { PROVIDER_LABELS, providerLabel } from '../../api/constants'
import type { AiModelEntry, AiStatus, AnalysisResultDict, MoondreamInstallStatus } from '../../api/types'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ status: AiStatus }>()

const toast = useToastStore()

const MODEL_PICKER_PROVIDERS = new Set(['ollama', 'ollama_cloud', 'anthropic', 'openai'])
const showModelPicker = () => MODEL_PICKER_PROVIDERS.has(props.status.provider || '')

// OpenAI's picker is sorted newest-to-oldest (see analyzer.py's
// _OPENAI_MODEL_DISPLAY_ORDER), not cheapest/best-first, so "index 0 is
// best" doesn't hold for it the way it does for Ollama (sorted by vision
// score) — these are the two models this add-on actually recommends: nano
// for tier 1 since it runs on every clip, mini for tier 2 since it only
// runs on clips tier 1 already flagged and can afford to be a bit stronger.
const OPENAI_BEST_PRIMARY_MODEL = 'gpt-5.4-nano'
const OPENAI_BEST_ESCALATION_MODEL = 'gpt-5.4-mini'

type ModelTier = 'primary' | 'escalation'

function isBestModel(m: AiModelEntry, index: number, provider: string | undefined, tier: ModelTier): boolean {
  if (provider === 'openai') {
    return m.name === (tier === 'primary' ? OPENAI_BEST_PRIMARY_MODEL : OPENAI_BEST_ESCALATION_MODEL)
  }
  return index === 0
}

function modelLabel(m: AiModelEntry, index: number, provider: string | undefined, tier: ModelTier): string {
  const gb = m.size ? ` · ${(m.size / 1e9).toFixed(1)} GB` : ''
  const star = isBestModel(m, index, provider, tier) ? ' ⭐ Best' : ''
  return `${m.name}${gb}${star}`
}

// ── Model picker (tier 1) ──────────────────────────────────
const models = ref<AiModelEntry[]>([])
const selectedModel = ref('')
const fetchingModels = ref(false)

async function fetchModels() {
  fetchingModels.value = true
  try {
    const d = await fetchAiModels()
    models.value = d.models || []
    if (models.value.length && !selectedModel.value) selectedModel.value = models.value[0].name
    toast.show(
      models.value.length
        ? `Found ${models.value.length} vision model(s)`
        : 'No vision models found on this Ollama server',
    )
  } catch {
    toast.show('Failed to fetch models', true)
  } finally {
    fetchingModels.value = false
  }
}

async function copyModelId() {
  if (!selectedModel.value) {
    toast.show('Fetch models and pick one first', true)
    return
  }
  try {
    await navigator.clipboard.writeText(selectedModel.value)
    toast.show(`Copied "${selectedModel.value}" — paste into the add-on Configuration tab`)
  } catch {
    toast.show(selectedModel.value, true)
  }
}

// ── Escalation (tier 2) model picker — mirrors the tier-1 picker above,
// but targets whichever provider is configured as ai_escalation_provider.
// Same limitation as tier 1: only works once escalation is actually
// attached/configured, and only copies the id for pasting into the add-on's
// Configuration tab rather than writing it live (config.yaml options aren't
// live-writable from this UI).
const escalationModels = ref<AiModelEntry[]>([])
const selectedEscalationModel = ref('')
const fetchingEscalationModels = ref(false)

async function fetchEscalationModelsList() {
  fetchingEscalationModels.value = true
  try {
    const d = await fetchEscalationModels()
    escalationModels.value = d.models || []
    if (escalationModels.value.length && !selectedEscalationModel.value) {
      selectedEscalationModel.value = escalationModels.value[0].name
    }
    toast.show(
      escalationModels.value.length
        ? `Found ${escalationModels.value.length} escalation model(s)`
        : d.error || 'No models found for the escalation provider',
      !escalationModels.value.length,
    )
  } catch {
    toast.show('Failed to fetch escalation models', true)
  } finally {
    fetchingEscalationModels.value = false
  }
}

async function copyEscalationModelId() {
  if (!selectedEscalationModel.value) {
    toast.show('Fetch models and pick one first', true)
    return
  }
  try {
    await navigator.clipboard.writeText(selectedEscalationModel.value)
    toast.show(`Copied "${selectedEscalationModel.value}" — paste into the AI Escalation Model field`)
  } catch {
    toast.show(selectedEscalationModel.value, true)
  }
}

// ── Moondream local install ───────────────────────────────
const archSupported = ref(true)
const installState = ref<{ status: MoondreamInstallStatus; log?: string }>({ status: 'idle' })
let pollTimer: ReturnType<typeof setTimeout> | undefined

async function pollMoondreamStatus() {
  try {
    const s = await getMoondreamInstallStatus()
    archSupported.value = s.arch_supported !== false
    installState.value = s.install_state || { status: 'idle' }
    if (s.installed) installState.value = { ...installState.value, status: 'installed' }
  } catch {
    // Transient — a single dropped poll must not kill the whole progress
    // tracker for what the UI itself says "may take several minutes".
    // Re-check below using whatever installState already holds (unchanged
    // by this failed attempt) so polling keeps going as long as we were
    // still mid-install.
  }
  if (installState.value.status === 'installing') {
    pollTimer = setTimeout(pollMoondreamStatus, 2500)
  }
}

async function startInstall() {
  const previousState = installState.value
  try {
    installState.value = { status: 'installing', log: 'Starting pip install moondream…\n' }
    await startMoondreamInstall()
    await pollMoondreamStatus()
  } catch {
    // Roll back the optimistic "installing" state so the Install/Retry
    // button reappears immediately — otherwise the panel is stuck showing
    // "Installing… please wait" forever, since no poll loop ever started
    // to self-correct it (the failure happened before pollMoondreamStatus
    // was ever reached).
    installState.value = previousState
    toast.show('Failed to start installation', true)
  }
}

watch(
  () => props.status.provider,
  (provider) => {
    if (provider === 'moondream_local') {
      archSupported.value = props.status.moondream_arch_supported !== false
      installState.value = props.status.moondream_installed ? { status: 'installed' } : { status: 'idle' }
      void pollMoondreamStatus()
    } else if (pollTimer) {
      clearTimeout(pollTimer)
    }
  },
  { immediate: true },
)

// The AI tab is v-if-gated (see App.vue) — fully destroyed on tab switch —
// so a poll started mid-install must be cleared here too, not just on the
// provider watch above (which only fires on a provider *change*, never on
// unmount). Without this, switching tabs during an install leaks the timer
// (and everything it closes over) forever.
onUnmounted(() => {
  if (pollTimer) clearTimeout(pollTimer)
})

// ── Test analysis ──────────────────────────────────────────
const testing = ref(false)
const testResult = ref<{ ok: boolean; message: string; detail?: AnalysisResultDict & { camera?: string } } | null>(null)

async function runTest() {
  testing.value = true
  testResult.value = null
  try {
    const clips = await listClips({ limit: 1, sort: 'newest' })
    if (!clips.length) {
      testResult.value = {
        ok: false,
        message: 'No clips in the library yet. Download a clip first, then run the test.',
      }
      return
    }
    const clip = clips[0]
    const r = await analyzeClipNow(clip.id)
    testResult.value = { ok: true, message: '', detail: { ...r, camera: r.camera || clip.camera } }
    toast.show('Test complete — AI is working')
  } catch {
    testResult.value = { ok: false, message: 'Test failed — check AI provider settings and connection' }
    toast.show('AI test failed', true)
  } finally {
    testing.value = false
  }
}

function confPct(r: AnalysisResultDict): number {
  return Math.round((r.confidence || 0) * 100)
}
</script>

<template>
  <div class="card" style="padding: 1.2rem">
    <h3 style="margin-bottom: 0.8rem">AI Connection</h3>
    <div style="display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.6rem">
      <span
        class="badge"
        style="font-size: 0.85rem"
        :style="{ color: status.ai_online ? 'var(--success)' : 'var(--danger)' }"
        >●</span
      >
      <span style="font-size: 0.85rem">{{ status.ai_online ? 'Connected' : 'Offline' }}</span>
    </div>
    <div class="ai-tier-label">🎯 Tier 1 · Primary Model</div>
    <div style="font-size: 0.82rem; color: var(--muted); margin-bottom: 0.4rem">
      Provider: <strong>{{ providerLabel(status.provider) }}</strong>
    </div>
    <div style="font-size: 0.82rem; color: var(--muted); margin-bottom: 0.6rem">
      Model: <strong>{{ status.model || '—' }}</strong>
    </div>

    <div v-if="showModelPicker()" class="model-picker">
      <button type="button" class="btn sm model-picker__fetch" :disabled="fetchingModels" @click="fetchModels">
        {{ fetchingModels ? '⏳ Loading…' : '⟳ Fetch Models' }}
      </button>
      <div class="model-picker__row">
        <label for="ai-model-picker" class="sr-only">Vision model</label>
        <select id="ai-model-picker" v-model="selectedModel" class="sel model-picker__select">
          <option value="">Select a model…</option>
          <option v-for="(m, i) in models" :key="m.name" :value="m.name">
            {{ modelLabel(m, i, status.provider, 'primary') }}
          </option>
        </select>
        <button
          type="button"
          class="btn sm ghost model-picker__copy"
          title="Copy the selected model id, then paste it into this add-on's configuration (OpenAI Model / Anthropic Model / Ollama Vision Model)"
          @click="copyModelId"
        >
          📋 Copy
        </button>
      </div>
    </div>
    <p v-if="showModelPicker()" style="font-size: 0.72rem; color: var(--muted); margin-top: 0.35rem">
      Selecting a model here does not change the running configuration — copy the id and paste it into the add-on's
      <strong>Configuration</strong> tab, then restart the add-on.
    </p>

    <div v-if="status.provider === 'moondream_local'" style="margin-top: 0.75rem">
      <div v-if="!archSupported">
        <p style="font-size: 0.8rem; color: var(--muted)">
          moondream_local is not available on this architecture.<br />Use <strong>moondream_cloud</strong> or
          <strong>ollama</strong> instead.
        </p>
      </div>
      <div v-else-if="installState.status === 'installed'">
        <p style="font-size: 0.8rem; color: var(--success)">✓ moondream installed</p>
        <p style="font-size: 0.73rem; color: var(--muted)">
          Model (~430 MB) downloads automatically on first health check
        </p>
      </div>
      <div v-else-if="installState.status === 'installing'">
        <p style="font-size: 0.8rem; color: var(--warn); margin-bottom: 0.35rem">⏳ Installing… please wait</p>
        <div
          style="
            font-size: 0.7rem;
            font-family: monospace;
            color: var(--muted);
            background: var(--card2);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 0.3rem 0.5rem;
            max-height: 70px;
            overflow-y: auto;
            white-space: pre-wrap;
          "
        >
          {{ installState.log || '' }}
        </div>
      </div>
      <div v-else-if="installState.status === 'failed'">
        <p style="font-size: 0.8rem; color: var(--danger); margin-bottom: 0.3rem">✗ Installation failed</p>
        <div
          style="
            font-size: 0.7rem;
            font-family: monospace;
            color: var(--muted);
            background: var(--card2);
            border: 1px solid var(--border);
            border-radius: 4px;
            padding: 0.3rem 0.5rem;
            max-height: 70px;
            overflow-y: auto;
            white-space: pre-wrap;
            margin-bottom: 0.4rem;
          "
        >
          {{ installState.log || '' }}
        </div>
        <button type="button" class="btn sm ghost" @click="startInstall">↺ Retry Install</button>
      </div>
      <div v-else>
        <p style="font-size: 0.8rem; color: var(--warn); margin-bottom: 0.45rem">⚠ moondream package not installed</p>
        <button type="button" class="btn sm" @click="startInstall">⬇ Install Moondream 0.5B</button>
        <p style="font-size: 0.73rem; color: var(--muted); margin-top: 0.35rem">
          Package + model ~430 MB, may take several minutes
        </p>
      </div>
    </div>

    <template v-if="status.escalation_provider">
      <div class="ai-tier-label" style="margin-top: 0.9rem">🪜 Tier 2 · Escalation Model</div>
      <div style="font-size: 0.82rem; color: var(--muted); margin-bottom: 0.4rem">
        Provider: <strong>{{ PROVIDER_LABELS[status.escalation_provider] || status.escalation_provider }}</strong>
      </div>
      <div style="font-size: 0.82rem; color: var(--muted); margin-bottom: 0.6rem">
        Model: <strong>{{ status.escalation_model || '—' }}</strong>
        <span :style="{ color: status.escalation_online ? 'var(--success)' : 'var(--danger)' }">
          {{ status.escalation_online ? ' 🟢 online' : ' 🔴 unreachable — falling back to tier 1' }}
        </span>
      </div>

      <div class="model-picker">
        <button
          type="button"
          class="btn sm model-picker__fetch"
          :disabled="fetchingEscalationModels"
          @click="fetchEscalationModelsList"
        >
          {{ fetchingEscalationModels ? '⏳ Loading…' : '⟳ Fetch Escalation Models' }}
        </button>
        <div class="model-picker__row">
          <label for="ai-escalation-model-picker" class="sr-only">Escalation model</label>
          <select id="ai-escalation-model-picker" v-model="selectedEscalationModel" class="sel model-picker__select">
            <option value="">Select a model…</option>
            <option v-for="(m, i) in escalationModels" :key="m.name" :value="m.name">
              {{ modelLabel(m, i, status.escalation_provider, 'escalation') }}
            </option>
          </select>
          <button
            type="button"
            class="btn sm ghost model-picker__copy"
            title="Copy the selected model id, then paste it into this add-on's configuration (AI Escalation Model)"
            @click="copyEscalationModelId"
          >
            📋 Copy
          </button>
        </div>
      </div>
    </template>

    <div style="margin-top: 0.75rem; border-top: 1px solid var(--border); padding-top: 0.6rem">
      <button type="button" class="btn sm ghost" :disabled="testing" @click="runTest">
        {{ testing ? '⏳ Testing…' : '🔬 Test Analysis' }}
      </button>
      <p style="font-size: 0.73rem; color: var(--muted); margin: 0.3rem 0 0">
        Analyzes a recent clip to verify AI is working
      </p>
      <div v-if="testResult" style="margin-top: 0.45rem">
        <span v-if="!testResult.ok" style="color: var(--warn); font-size: 0.8rem">{{ testResult.message }}</span>
        <div
          v-else-if="testResult.detail"
          style="
            background: var(--card2);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 0.6rem 0.8rem;
            font-size: 0.82rem;
            margin-top: 0.3rem;
          "
        >
          <div style="color: var(--success); font-weight: 700; margin-bottom: 0.35rem">✓ AI is working!</div>
          <div>
            Camera: <strong>{{ testResult.detail.camera }}</strong>
          </div>
          <div>
            Result:
            <span
              :style="{ color: testResult.detail.is_suspicious ? 'var(--danger)' : 'var(--success)', fontWeight: 600 }"
            >
              {{ testResult.detail.is_suspicious ? '⚠ Suspicious' : '✓ Clear' }}
            </span>
            ({{ confPct(testResult.detail) }}% confidence)
          </div>
          <div v-if="testResult.detail.summary" style="color: var(--muted); margin-top: 0.3rem">
            {{ testResult.detail.summary }}
          </div>
          <div style="color: var(--muted); font-size: 0.74rem; margin-top: 0.3rem">
            Model: {{ testResult.detail.model || '—' }} &nbsp;·&nbsp; {{ testResult.detail.frame_count || 0 }} frame(s)
            &nbsp;·&nbsp; {{ (testResult.detail.analysis_duration || 0).toFixed(1) }}s
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* Small uppercase divider label, matching the sidebar's existing
   .app-nav-section-label treatment — separates tier-1 (primary) model
   controls from tier-2 (escalation) ones, which previously had no visual
   grouping distinguishing them from each other. */
.ai-tier-label {
  font-size: 0.68rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  padding-bottom: 0.3rem;
  margin-bottom: 0.5rem;
}

/* Shared by the tier-1 and tier-2 model pickers so both look and behave
   identically: the fetch button always sits on its own line, with the
   select + copy button always on the row below it. Previously the fetch
   button, select, and copy button shared one flex-wrap row, so the select
   (whose width followed its longest fetched option) started out beside the
   fetch button but reflowed onto its own line the moment real options
   arrived — the layout jumped around every time models were fetched. */
.model-picker {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.5rem;
}

.model-picker__row {
  display: flex;
  width: 100%;
  gap: 0.5rem;
  align-items: center;
  flex-wrap: wrap;
}

.model-picker__select {
  flex: 1 1 175px;
  min-width: 175px;
}
</style>
