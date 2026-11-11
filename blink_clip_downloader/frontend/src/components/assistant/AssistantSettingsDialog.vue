<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Dialog from 'primevue/dialog'
import Message from 'primevue/message'
import Select from 'primevue/select'
import SelectButton from 'primevue/selectbutton'
import { getAssistantModels, saveAssistantSettings } from '../../api/assistant'
import { describeApiError } from '../../api/client'
import { providerLabel } from '../../api/constants'
import type { AssistantSettings, AssistantStatus } from '../../api/types'
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ status: AssistantStatus | null }>()
const visible = defineModel<boolean>('visible', { required: true })
const emit = defineEmits<{ saved: [settings: AssistantSettings] }>()

const toast = useToastStore()

const models = ref<string[]>([])
const loadingModels = ref(false)
const modelsFailed = ref(false)
const saving = ref(false)
const model = ref('')
const days = ref(7)

const DEFAULT_CHOICE = ''

const modelOptions = computed(() => [
  { label: `Provider default (${props.status?.default_model || 'not set'})`, value: DEFAULT_CHOICE },
  ...models.value.map((name) => ({ label: name, value: name })),
])
const dayOptions = computed(() =>
  (props.status?.days_choices || [7]).map((n) => ({ label: n === 1 ? '1 day' : `${n} days`, value: n })),
)

async function loadModels() {
  loadingModels.value = true
  modelsFailed.value = false
  try {
    const r = await getAssistantModels()
    models.value = r.models
    modelsFailed.value = r.models.length === 0
  } catch {
    models.value = []
    modelsFailed.value = true
  } finally {
    loadingModels.value = false
  }
}

// Reopening always starts from what is saved, never from a half-edited draft.
watch(visible, (open) => {
  if (!open) return
  model.value = props.status?.settings.model ?? DEFAULT_CHOICE
  days.value = props.status?.settings.days ?? 7
  void loadModels()
})

async function save() {
  saving.value = true
  try {
    const r = await saveAssistantSettings({ model: model.value, days: days.value })
    toast.show('Assistant settings saved')
    emit('saved', r.settings)
    visible.value = false
  } catch (e) {
    toast.show(describeApiError(e, 'Could not save the assistant settings'), true)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Dialog
    v-model:visible="visible"
    modal
    dismissable-mask
    header="Assistant settings"
    :style="{ width: '30rem', maxWidth: 'calc(100vw - 2rem)' }"
    :draggable="false"
  >
    <div class="settings-form" data-testid="assistant-settings">
      <p class="settings-note">
        The assistant uses your main AI provider,
        <strong>{{ providerLabel(status?.provider) }}</strong
        >, the one that analyzes your clips, not the escalation provider. Pick which of its models answers your
        questions.
      </p>

      <div class="settings-field">
        <label for="assistant-model">Model</label>
        <Select
          v-model="model"
          input-id="assistant-model"
          :options="modelOptions"
          option-label="label"
          option-value="value"
          :loading="loadingModels"
          filter
          fluid
          placeholder="Choose a model"
          data-testid="assistant-model-select"
        />
        <Message v-if="modelsFailed && !loadingModels" severity="warn" size="small" variant="simple">
          The provider's model list could not be loaded, so only its default is offered. Check that the provider is
          reachable on the AI tab.
        </Message>
      </div>

      <div class="settings-field">
        <span id="assistant-days-label">How far back it looks</span>
        <SelectButton
          v-model="days"
          :options="dayOptions"
          option-label="label"
          option-value="value"
          :allow-empty="false"
          size="small"
          aria-labelledby="assistant-days-label"
          data-testid="assistant-days"
        />
        <small class="settings-hint">
          How many days of clips and events it reads for each question. A longer window costs more tokens.
        </small>
      </div>
    </div>
    <template #footer>
      <Button label="Cancel" size="small" severity="secondary" text @click="visible = false" />
      <Button
        label="Save"
        size="small"
        icon="pi pi-check"
        :loading="saving"
        data-testid="assistant-settings-save"
        @click="save"
      />
    </template>
  </Dialog>
</template>

<style scoped>
.settings-form {
  display: flex;
  flex-direction: column;
  gap: 1.1rem;
}
.settings-note {
  margin: 0;
  color: var(--text-dim);
  line-height: 1.5;
}
.settings-field {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.settings-field label,
.settings-field > span {
  font-weight: 600;
  font-size: 0.9rem;
}
.settings-hint {
  color: var(--muted);
}
</style>
