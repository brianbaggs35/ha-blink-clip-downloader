<script setup lang="ts">
import { onMounted, ref } from 'vue'
import Button from 'primevue/button'
import Card from 'primevue/card'
import Select from 'primevue/select'
import {
  activateCheckpoint,
  createFinetune,
  deleteFinetune,
  listCheckpoints,
  listFinetunes,
  saveCheckpoint,
  trainFromFeedback,
  getUntrainedFeedbackCount,
} from '../../api/ai'
import type { MoondreamCheckpoint, MoondreamFinetune } from '../../api/types'
import { useConfirm } from '../../composables/useConfirm'
import { useToastStore } from '../../stores/toast'

const emit = defineEmits<{ activated: [] }>()

const RANK_OPTIONS = [
  { label: 'Rank 8', value: 8 },
  { label: 'Rank 16', value: 16 },
  { label: 'Rank 24', value: 24 },
  { label: 'Rank 32', value: 32 },
]

const toast = useToastStore()
const confirm = useConfirm()

const loading = ref(true)
const loadError = ref(false)
const finetunes = ref<MoondreamFinetune[]>([])
const pendingCount = ref(0)
const view = ref<'list' | 'checkpoints'>('list')
const checkpointsFinetuneId = ref('')
const checkpoints = ref<MoondreamCheckpoint[]>([])
const newName = ref('')
const newRank = ref(16)

function finetuneId(ft: MoondreamFinetune): string {
  return ft.finetune_id || ft.id || ''
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    const [d, feedback] = await Promise.all([listFinetunes(), getUntrainedFeedbackCount().catch(() => ({ count: 0 }))])
    finetunes.value = d.finetunes || []
    pendingCount.value = feedback.count || 0
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}
onMounted(load)

async function train(id: string) {
  try {
    const r = await trainFromFeedback(id)
    toast.show(
      r.trained > 0
        ? `Trained ${r.trained} step(s) — Save Checkpoint to make it activatable`
        : r.message || 'No new feedback to train on',
    )
    await load()
  } catch {
    toast.show('Training failed', true)
  }
}

async function saveCkpt(id: string) {
  try {
    const r = await saveCheckpoint(id)
    toast.show(r.saved ? 'Checkpoint saved' : 'Failed to save checkpoint', !r.saved)
  } catch {
    toast.show('Failed to save checkpoint', true)
  }
}

async function create() {
  const name = newName.value.trim()
  if (!name) {
    toast.show('Enter a name for the fine-tune', true)
    return
  }
  try {
    await createFinetune(name, newRank.value)
    toast.show('Fine-tune created')
    newName.value = ''
    await load()
  } catch {
    toast.show('Failed to create fine-tune', true)
  }
}

async function remove(id: string) {
  if (!(await confirm('Delete this fine-tune and all its checkpoints? This cannot be undone.', 'Delete fine-tune?')))
    return
  try {
    await deleteFinetune(id)
    toast.show('Fine-tune deleted')
    await load()
  } catch {
    toast.show('Failed to delete fine-tune', true)
  }
}

async function viewCheckpoints(id: string) {
  try {
    const d = await listCheckpoints(id)
    if (!d.checkpoints.length) {
      toast.show('No checkpoints saved yet for this fine-tune')
      return
    }
    checkpointsFinetuneId.value = id
    checkpoints.value = d.checkpoints
    view.value = 'checkpoints'
  } catch {
    toast.show('Failed to load checkpoints', true)
  }
}

async function activate(step: number) {
  try {
    const r = await activateCheckpoint(checkpointsFinetuneId.value, step)
    toast.show(`Activated: ${r.model}`)
    emit('activated')
  } catch {
    toast.show('Failed to activate checkpoint', true)
  }
}

function backToList() {
  view.value = 'list'
}
</script>

<template>
  <Card>
    <template #title><h3 style="margin: 0; font: inherit; color: inherit">🎯 Fine-Tuning</h3></template>
    <template #content>
      <div
        v-if="view === 'checkpoints'"
        style="display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.6rem"
      >
        <div
          v-for="cp in checkpoints"
          :key="cp.step"
          style="
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.5rem;
            padding: 0.4rem 0.55rem;
            background: var(--card2);
            border-radius: var(--radius);
          "
        >
          <span style="font-size: 0.82rem">Step {{ cp.step }}</span>
          <Button size="small" @click="activate(cp.step)">Activate</Button>
        </div>
        <Button size="small" severity="secondary" outlined style="margin-top: 0.4rem" @click="backToList">
          ← Back to fine-tunes
        </Button>
      </div>

      <template v-else>
        <div style="display: flex; flex-direction: column; gap: 0.4rem; margin-bottom: 0.6rem">
          <div v-if="loading" style="color: var(--muted); font-size: 0.8rem">Loading…</div>
          <div v-else-if="loadError" style="color: var(--danger); font-size: 0.8rem">Failed to load fine-tunes</div>
          <div v-else-if="!finetunes.length" style="color: var(--muted); font-size: 0.8rem">
            No fine-tunes yet — create one below.
          </div>
          <template v-else>
            <div
              v-for="ft in finetunes"
              :key="finetuneId(ft)"
              style="
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 0.5rem;
                padding: 0.4rem 0.55rem;
                background: var(--card2);
                border-radius: var(--radius);
                flex-wrap: wrap;
              "
            >
              <span style="font-size: 0.82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{
                ft.name || finetuneId(ft) || '—'
              }}</span>
              <div style="display: flex; gap: 0.3rem; flex-shrink: 0; flex-wrap: wrap">
                <Button
                  size="small"
                  severity="secondary"
                  outlined
                  title="Train on corrections from clip feedback"
                  @click="train(finetuneId(ft))"
                >
                  🧠 Train from Feedback ({{ pendingCount }})
                </Button>
                <Button
                  size="small"
                  severity="secondary"
                  outlined
                  title="Save current trained state as an activatable checkpoint"
                  @click="saveCkpt(finetuneId(ft))"
                >
                  💾 Save Checkpoint
                </Button>
                <Button size="small" severity="secondary" outlined @click="viewCheckpoints(finetuneId(ft))"
                  >Checkpoints</Button
                >
                <Button size="small" severity="danger" @click="remove(finetuneId(ft))">🗑</Button>
              </div>
            </div>
          </template>
        </div>
        <div style="display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap">
          <label for="finetune-new-name" class="sr-only">New fine-tune name</label>
          <input id="finetune-new-name" v-model="newName" class="tag-input" placeholder="New fine-tune name" />
          <label for="finetune-new-rank" class="sr-only">Fine-tune rank</label>
          <Select
            id="finetune-new-rank"
            v-model="newRank"
            :options="RANK_OPTIONS"
            option-label="label"
            option-value="value"
            size="small"
          />
          <Button size="small" @click="create">+ New Fine-tune</Button>
        </div>
        <p style="font-size: 0.72rem; color: var(--muted); margin-top: 0.4rem">
          🧠 Train from Feedback turns your 👍/👎 clip corrections into real training steps against Moondream Cloud. 💾
          Save Checkpoint persists the result so it appears under Checkpoints — Activate one to switch live inference
          immediately, no restart needed.
        </p>
      </template>
    </template>
  </Card>
</template>
