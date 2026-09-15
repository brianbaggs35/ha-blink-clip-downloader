<script setup lang="ts">
import { computed, ref } from 'vue'
import Button from 'primevue/button'
import Chip from 'primevue/chip'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import Tag from 'primevue/tag'
import {
  analyzeClipNow,
  deleteFeedback,
  getClipAiResult,
  getFeedbackForClip,
  listFaces,
  submitFaceRecognitionFeedback,
  submitFeedback,
} from '../../api/ai'
import type { AnalysisResultDict, DetectedObjectSummary, FaceFeedbackReportType, Feedback } from '../../api/types'
import {
  evidenceLabel,
  formatEventType,
  formatOffset,
  severityColor,
  severityLabel,
  severityRank,
  severityTag,
} from '../security/severity'
import { usePromptOverlayStore } from '../../stores/promptOverlay'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ clipId: string; promptDebugEnabled?: boolean }>()

const toast = useToastStore()
const promptOverlay = usePromptOverlayStore()
const refresh = useRefreshStore()

const expanded = ref(false)
const loading = ref(false)
const loadError = ref(false)
const result = ref<AnalysisResultDict | null>(null)
const loaded = ref(false)
const feedback = ref<Feedback | null>(null)
const feedbackClearing = ref(false)
const showRawResponse = ref(false)
const showFeedbackForm = ref(false)
const feedbackNote = ref('')
const feedbackCorrectedSuspicious = ref(false)
const analyzing = ref(false)
const faceReportSubmitting = ref(false)
const faceReportSubmitted = ref<FaceFeedbackReportType | null>(null)
const enrolledNames = ref<string[]>([])

/** Most severe first, then earliest: the panel has room for a few lines, and
 *  a list led by "a person was visible" buries the reason the clip matters.
 *  The full, time-ordered list lives on the Security tab. */
const securityEvents = computed(() =>
  [...(result.value?.security_events ?? [])]
    .sort((a, b) => severityRank(b.severity) - severityRank(a.severity) || a.start_offset - b.start_offset)
    .slice(0, 4),
)

/** Present only when the optional detection pipeline ran and the security
 *  layer found something — an empty section would otherwise appear on every
 *  clip for anyone with the feature off. */
const hasSecurityAssessment = computed(() => securityEvents.value.length > 0)
const showFaceReportPicker = ref(false)
const faceReportPersonName = ref('')
const faceReportType = ref<FaceFeedbackReportType>('false_negative')

async function load() {
  loading.value = true
  loadError.value = false
  try {
    result.value = await getClipAiResult(props.clipId)
    feedback.value = result.value ? await getFeedbackForClip(props.clipId).catch(() => null) : null
    enrolledNames.value = result.value ? await loadEnrolledNames() : []
    faceReportSubmitted.value = null
    showFaceReportPicker.value = false
    faceReportType.value = 'false_negative'
    loaded.value = true
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

async function loadEnrolledNames(): Promise<string[]> {
  try {
    const { faces } = await listFaces()
    return [...new Set(faces.map((f) => f.name))]
  } catch {
    return []
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
    // Deliberately not setting loadError here: the analyze request itself
    // never reached the server, so whatever was already on screen (the
    // previous result, or the "Not analyzed yet" prompt) is still accurate
    // and still lets the user retry. Setting loadError would replace that
    // with a dead-end "Failed to load analysis" message — loaded stays
    // true from the earlier successful load, so toggle() never calls
    // load() again, and the panel would be stuck until fully remounted.
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
  // The checkbox always proposes the opposite of the clip's current
  // verdict — "incorrect" only ever has one valid correction (see
  // media_server.py's _handle_ai_feedback_submit) — so which boolean it
  // sends flips with is_suspicious rather than always being true.
  const proposedSuspicious = !(result.value?.is_suspicious ?? false)
  const ok = await trySubmitFeedback({
    correct: false,
    correction_note: feedbackNote.value,
    corrected_suspicious: feedbackCorrectedSuspicious.value ? proposedSuspicious : undefined,
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
    // The AI tab's AdaptiveLearningCard/SuspiciousFeed can be mounted in the
    // background right now (this panel opens from a clip modal that can be
    // reached from the AI tab's Suspicious Activity Feed without switching
    // tabs) — bump the shared refresh signal so their stats/list don't go
    // stale until the AI tab is fully unmounted/remounted.
    refresh.bump()
    return true
  } catch {
    toast.show('Failed to save feedback', true)
    return false
  }
}

function changeFeedback() {
  feedback.value = null
}

async function clearFeedback() {
  feedbackClearing.value = true
  try {
    await deleteFeedback(props.clipId)
    toast.show('Feedback cleared')
    refresh.bump()
    feedback.value = null
  } catch {
    toast.show('Failed to clear feedback', true)
  } finally {
    feedbackClearing.value = false
  }
}

function startFaceReport(reportType: FaceFeedbackReportType) {
  faceReportType.value = reportType
  // No ambiguity to resolve unless there's more than one enrolled person —
  // with 0 or 1 names on file, submit immediately (auto-attaching the sole
  // name when there is one) instead of making the user pick from a list of
  // one.
  if (enrolledNames.value.length > 1) {
    faceReportPersonName.value = ''
    showFaceReportPicker.value = true
    return
  }
  void reportFaceIssue(reportType, enrolledNames.value[0] ?? '')
}

async function reportFaceIssue(reportType: FaceFeedbackReportType, personName = '') {
  faceReportSubmitting.value = true
  try {
    await submitFaceRecognitionFeedback(props.clipId, reportType, '', personName)
    faceReportSubmitted.value = reportType
    showFaceReportPicker.value = false
    toast.show('Thanks, reported — visible on the Biometrics activity card')
  } catch {
    toast.show('Failed to save the report', true)
  } finally {
    faceReportSubmitting.value = false
  }
}

function showPrompt() {
  promptOverlay.show(result.value?.prompt_text || '')
}

const confPct = (r: AnalysisResultDict) => Math.round((r.confidence || 0) * 100)

// One emoji per label the backend's object-detection stage can report
// (vision.py's _RELEVANT_CLASSES) — a fallback covers any future/unknown
// label so a chip still renders instead of silently disappearing.
const DETECTION_EMOJI: Record<string, string> = {
  person: '🧍',
  car: '🚗',
  truck: '🚚',
  bus: '🚌',
  motorcycle: '🏍️',
  bicycle: '🚲',
  dog: '🐕',
  cat: '🐈',
  bird: '🐦',
  horse: '🐴',
  backpack: '🎒',
  handbag: '👜',
  suitcase: '🧳',
}
const detectionEmoji = (label: string) => DETECTION_EMOJI[label] ?? '📦'

// Irregular plurals for the labels the detector can report; everything
// else takes a plain "s".
const DETECTION_PLURAL: Record<string, string> = { person: 'people', bus: 'buses' }
const detectionNoun = (label: string, count: number) => (count === 1 ? label : (DETECTION_PLURAL[label] ?? `${label}s`))

/** "3 cars" — the bare number the chips used to carry said nothing about
 *  what was counted, which mattered most for the labels whose emoji is
 *  ambiguous (and for the generic 📦 fallback, which names nothing at all). */
const detectionLabel = (obj: DetectedObjectSummary) => `${obj.count} ${detectionNoun(obj.label, obj.count)}`

/** The full story behind a chip's number: what it counts, how much raw
 *  evidence sits behind it, and how sure the detector was at its best. */
function detectionTitle(obj: DetectedObjectSummary): string {
  const noun = detectionNoun(obj.label, obj.count)
  const parts = [`${obj.count} distinct ${noun} tracked across the clip`]
  if (obj.detections) parts.push(`${obj.detections} detection(s) over the sampled frames`)
  parts.push(`up to ${Math.round(obj.max_confidence * 100)}% confidence`)
  return parts.join(' · ')
}

const faceReportNameOptions = computed(() => [
  { label: 'Not sure / someone else', value: '' },
  ...enrolledNames.value.map((n) => ({ label: n, value: n })),
])
</script>

<template>
  <div class="ai-panel">
    <button type="button" class="ai-panel-hdr" :class="{ open: expanded }" @click="toggle">
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
      <div class="ai-panel-inner">
        <span v-if="loading" class="ai-panel-status">Loading…</span>
        <span v-else-if="loadError" class="ai-panel-status is-error">Failed to load analysis</span>
        <template v-else-if="loaded && !result">
          <div class="ai-panel-status ai-panel-status-block">Not analyzed yet</div>
          <Button size="small" :disabled="analyzing" @click="analyzeNow">
            {{ analyzing ? '⏳ Analyzing…' : '🔬 Analyze Now' }}
          </Button>
        </template>
        <div v-else-if="result" class="ai-result-box">
          <!-- The verdict, its confidence and the model's own sentence read
               as one statement, so they are banded together and tinted by
               the verdict rather than being three loose lines. -->
          <div class="ai-verdict" :class="result.is_suspicious ? 'is-suspicious' : 'is-clear'">
            <div class="ai-verdict-head">
              <Tag v-if="result.is_suspicious" severity="danger" value="⚠ Suspicious" class="ai-badge-suspicious" />
              <Tag v-else severity="success" value="✓ Clear" class="ai-badge-clean" />
              <span class="ai-verdict-confidence">{{ confPct(result) }}% confidence</span>
              <span class="ai-verdict-meter" aria-hidden="true">
                <span class="ai-verdict-meter-fill" :style="{ width: `${confPct(result)}%` }" />
              </span>
            </div>
            <p v-if="result.summary" class="ai-verdict-summary">{{ result.summary }}</p>
          </div>

          <p class="ai-meta">
            Model: {{ result.model || '—' }}
            <template v-if="result.analyzed_at">
              &nbsp;·&nbsp; {{ new Date(result.analyzed_at).toLocaleString() }}</template
            >
            <template v-if="result.frame_count"> &nbsp;·&nbsp; {{ result.frame_count }} frame(s) analyzed</template>
          </p>

          <section v-if="result.detected_objects?.length" class="ai-section">
            <h4 class="ai-section-title">What was detected</h4>
            <div class="ai-chips">
              <Chip
                v-for="obj in result.detected_objects"
                :key="obj.label"
                :label="`${detectionEmoji(obj.label)} ${detectionLabel(obj)}`"
                class="detection-chip"
                :title="detectionTitle(obj)"
              />
            </div>
          </section>

          <!-- The risk bar is tinted by the very band named in the tag
               beside it, from the same table the Security Events tab
               colours its rows with. -->
          <section
            v-if="hasSecurityAssessment"
            class="ai-section ai-security"
            data-testid="ai-security"
            :style="{ '--sev': severityColor(result.severity ?? 'routine') }"
          >
            <div class="ai-section-head">
              <h4 class="ai-section-title">Security evidence</h4>
              <Tag
                :value="`${severityLabel(result.severity ?? 'routine')} · risk ${Math.round(result.risk_score ?? 0)}`"
                :severity="severityTag(result.severity ?? 'routine')"
              />
            </div>
            <div class="ai-scores">
              <div class="ai-score">
                <span class="ai-score-label">Risk</span>
                <span class="ai-score-value">{{ Math.round(result.risk_score ?? 0) }}<small>/100</small></span>
                <span class="ai-score-track">
                  <span class="ai-score-fill is-risk" :style="{ width: `${Math.round(result.risk_score ?? 0)}%` }" />
                </span>
              </div>
              <div class="ai-score">
                <span class="ai-score-label">Evidence</span>
                <span class="ai-score-value">
                  {{ Math.round((result.evidence_quality ?? 0) * 100)
                  }}<small>% {{ evidenceLabel(result.evidence_quality ?? 0) }}</small>
                </span>
                <span class="ai-score-track">
                  <span
                    class="ai-score-fill"
                    :style="{ width: `${Math.round((result.evidence_quality ?? 0) * 100)}%` }"
                  />
                </span>
              </div>
            </div>
            <p v-if="result.risk_override_applied" class="ai-security-override">
              Flagged on detection evidence — the AI model itself reported nothing unusual.
            </p>
            <ul class="ai-security-list">
              <li v-for="event in securityEvents" :key="event.id">
                <span class="ai-security-time">{{ formatOffset(event.start_offset) }}</span>
                <Tag :value="formatEventType(event.event_type)" :severity="severityTag(event.severity)" />
                <span class="ai-security-text">{{ event.detail }}</span>
              </li>
            </ul>
          </section>

          <div class="ai-actions">
            <Button size="small" severity="secondary" outlined :disabled="analyzing" @click="analyzeNow"
              >↺ Re-analyze</Button
            >
            <Button size="small" severity="secondary" outlined @click="showRawResponse = !showRawResponse">
              {{ showRawResponse ? '📄 Hide response' : '📄 Full response' }}
            </Button>
            <Button v-if="promptDebugEnabled" size="small" severity="secondary" outlined @click="showPrompt"
              >📝 Prompt</Button
            >
          </div>
          <pre v-if="showRawResponse" class="ai-raw">{{ result.response_text || '' }}</pre>

          <section class="ai-section ai-footer-section">
            <h4 class="ai-section-title">Verdict feedback</h4>
            <div v-if="feedback" class="ai-feedback-row">
              <span v-if="feedback.correct" class="ai-feedback-given is-correct">👍 Marked correct</span>
              <span v-else class="ai-feedback-given is-incorrect"
                >👎 Marked incorrect<template v-if="feedback.correction_note">
                  — "{{ feedback.correction_note }}"</template
                ></span
              >
              <Button size="small" severity="secondary" outlined @click="changeFeedback">Change</Button>
              <Button size="small" severity="secondary" outlined :disabled="feedbackClearing" @click="clearFeedback">
                Clear
              </Button>
            </div>
            <div v-else class="ai-feedback-row">
              <span class="ai-feedback-ask">Was this verdict correct?</span>
              <Button size="small" severity="secondary" outlined @click="quickFeedback(true)">👍 Correct</Button>
              <Button size="small" severity="secondary" outlined @click="openFeedbackNoteForm">👎 Incorrect</Button>
            </div>
            <div v-if="showFeedbackForm" class="ai-feedback-form">
              <label for="clip-ai-feedback-note" class="sr-only">Feedback note</label>
              <InputText
                id="clip-ai-feedback-note"
                v-model="feedbackNote"
                class="tag-input"
                placeholder="What actually happened? (optional)"
                fluid
              />
              <label class="ai-feedback-check">
                <input v-model="feedbackCorrectedSuspicious" type="checkbox" />
                {{
                  result.is_suspicious
                    ? 'Should not have been flagged suspicious'
                    : 'Should have been flagged suspicious instead'
                }}
              </label>
              <div class="ai-feedback-form-actions">
                <Button size="small" @click="submitFeedbackFormClick">Submit</Button>
                <Button size="small" severity="secondary" outlined @click="showFeedbackForm = false">Cancel</Button>
              </div>
            </div>
          </section>

          <section class="ai-section ai-footer-section">
            <h4 class="ai-section-title">Face recognition</h4>
            <div v-if="faceReportSubmitted" class="ai-face-done">✓ Reported — thanks</div>
            <div v-else-if="result.face_bypass_applied" class="ai-face-row">
              <span class="ai-face-ask">👤 Face match ({{ result.face_bypass_names }}) — correct?</span>
              <Button
                size="small"
                severity="secondary"
                outlined
                :disabled="faceReportSubmitting"
                @click="reportFaceIssue('false_positive', result.face_bypass_names)"
              >
                👎 Wrong match
              </Button>
            </div>
            <div v-else-if="showFaceReportPicker" class="ai-face-row">
              <label for="clip-ai-face-report-name" class="sr-only">
                {{ faceReportType === 'false_negative' ? 'Who was missed?' : 'Who was wrongly matched?' }}
              </label>
              <Select
                id="clip-ai-face-report-name"
                v-model="faceReportPersonName"
                size="small"
                :options="faceReportNameOptions"
                option-label="label"
                option-value="value"
              />
              <Button
                size="small"
                severity="secondary"
                outlined
                :disabled="faceReportSubmitting"
                @click="reportFaceIssue(faceReportType, faceReportPersonName)"
              >
                {{ faceReportType === 'false_negative' ? '🚩' : '👎' }} Submit report
              </Button>
              <Button size="small" severity="secondary" outlined @click="showFaceReportPicker = false">Cancel</Button>
            </div>
            <div v-else class="ai-face-row">
              <Button
                size="small"
                severity="secondary"
                outlined
                :disabled="faceReportSubmitting"
                @click="startFaceReport('false_negative')"
              >
                🚩 Report a missed face match
              </Button>
              <Button
                size="small"
                severity="secondary"
                outlined
                :disabled="faceReportSubmitting"
                @click="startFaceReport('false_positive')"
              >
                👎 Wrong match
              </Button>
            </div>
          </section>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.ai-panel-inner {
  /* The grid collapse in base.css animates this element's height, which
     needs it to be able to shrink to nothing. */
  overflow: hidden;
  min-height: 0;
  padding-bottom: 0.3rem;
}
.ai-panel-status {
  font-size: 0.8rem;
  color: var(--muted);
}
.ai-panel-status.is-error {
  color: var(--danger);
}
.ai-panel-status-block {
  display: block;
  margin-bottom: 0.45rem;
}

/* ── Verdict banner ─────────────────────────────────────── */
.ai-verdict {
  border-radius: var(--radius-sm);
  border: 1px solid var(--border);
  border-left: 3px solid var(--muted);
  background: var(--card);
  padding: 0.55rem 0.7rem;
}
.ai-verdict.is-suspicious {
  border-left-color: var(--danger);
}
.ai-verdict.is-clear {
  border-left-color: var(--success);
}
.ai-verdict-head {
  display: flex;
  align-items: center;
  gap: 0.55rem;
  flex-wrap: wrap;
}
.ai-verdict-confidence {
  font-weight: 700;
  font-size: 0.8rem;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
/* How sure, as a shape rather than only a number — the percentage on its
   own gave no sense of where it sat on the scale. */
.ai-verdict-meter {
  flex: 1 1 80px;
  min-width: 60px;
  max-width: 180px;
  height: 4px;
  border-radius: 999px;
  background: var(--card-hover);
  overflow: hidden;
}
.ai-verdict-meter-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--muted);
}
.is-suspicious .ai-verdict-meter-fill {
  background: var(--danger);
}
.is-clear .ai-verdict-meter-fill {
  background: var(--success);
}
.ai-verdict-summary {
  margin: 0.4rem 0 0;
  color: var(--text);
  line-height: 1.45;
}
.ai-meta {
  margin: 0.4rem 0 0;
  color: var(--muted);
  font-size: 0.74rem;
}

/* ── Sections ───────────────────────────────────────────── */
.ai-section {
  margin-top: 0.75rem;
}
.ai-section-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-bottom: 0.4rem;
}
.ai-section-title {
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.09em;
  text-transform: uppercase;
  color: var(--muted);
  margin: 0 0 0.35rem;
}
.ai-section-head .ai-section-title {
  margin: 0;
}
/* Ruled off from the analysis itself: everything below is about telling
   the add-on it got something wrong, not about this clip. */
.ai-footer-section {
  margin-top: 0.7rem;
  padding-top: 0.6rem;
  border-top: 1px solid var(--border);
}
.ai-chips {
  display: flex;
  gap: 0.3rem;
  flex-wrap: wrap;
}

/* ── Security evidence ──────────────────────────────────── */
.ai-scores {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(170px, 100%), 1fr));
  gap: 0.35rem 1rem;
  margin-bottom: 0.5rem;
}
.ai-score {
  display: grid;
  grid-template-columns: auto 1fr;
  align-items: baseline;
  gap: 0.1rem 0.45rem;
}
.ai-score-label {
  font-size: 0.68rem;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
}
.ai-score-value {
  font-size: 0.9rem;
  font-weight: 700;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.ai-score-value small {
  font-size: 0.7rem;
  font-weight: 500;
  color: var(--muted);
}
.ai-score-track {
  grid-column: 1 / -1;
  height: 4px;
  border-radius: 999px;
  background: var(--card-hover);
  overflow: hidden;
  margin-top: 0.12rem;
}
.ai-score-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
}
.ai-score-fill.is-risk {
  background: var(--sev, var(--warn));
}
.ai-security-override {
  margin: 0 0 0.45rem;
  font-size: 0.75rem;
  color: var(--warn);
}
/* These rows had no styles at all before — they rendered as a default
   browser bullet list whose long detail text wrapped back under the
   marker, which is most of what made this panel look unfinished. */
.ai-security-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.ai-security-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.78rem;
  line-height: 1.4;
  color: var(--text-dim);
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.35rem 0.5rem;
}
.ai-security-time {
  font-variant-numeric: tabular-nums;
  font-size: 0.72rem;
  color: var(--muted);
  background: var(--card2);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  padding: 0.05rem 0.4rem;
}
.ai-security-text {
  flex: 1 1 min(280px, 100%);
}

/* ── Actions, raw response, feedback ────────────────────── */
.ai-actions {
  display: flex;
  gap: 0.4rem;
  align-items: center;
  flex-wrap: wrap;
  margin-top: 0.75rem;
}
.ai-raw {
  margin: 0.4rem 0 0;
  font-size: 0.73rem;
  font-family: monospace;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 0.45rem 0.55rem;
  white-space: pre-wrap;
  /* overflow-wrap is the standard property for this — word-break:
     break-word was only ever a non-standard value browsers happened to
     treat the same way (same note as base.css's .status-row .val.wrap). */
  overflow-wrap: break-word;
  color: var(--muted);
  max-height: 160px;
  overflow-y: auto;
}
.ai-feedback-row,
.ai-face-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-wrap: wrap;
  font-size: 0.78rem;
}
.ai-feedback-ask {
  color: var(--muted);
}
.ai-feedback-given.is-correct {
  color: var(--success);
}
.ai-feedback-given.is-incorrect {
  color: var(--warn);
}
.ai-feedback-form {
  margin-top: 0.45rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.ai-feedback-check {
  font-size: 0.75rem;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 0.3rem;
}
.ai-feedback-form-actions {
  display: flex;
  gap: 0.4rem;
}
.ai-face-ask {
  color: var(--muted);
}
.ai-face-done {
  font-size: 0.76rem;
  color: var(--success);
}
</style>
