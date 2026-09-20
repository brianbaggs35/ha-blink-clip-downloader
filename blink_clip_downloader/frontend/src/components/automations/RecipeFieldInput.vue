<script setup lang="ts">
import { computed } from 'vue'
import InputMask from 'primevue/inputmask'
import InputNumber from 'primevue/inputnumber'
import InputText from 'primevue/inputtext'
import MultiSelect from 'primevue/multiselect'
// Imported under a name that is not an HTML tag: a template parsed as HTML
// reads `<Select>` as `<select>` and then cannot see that the label's
// `:for` and the component's `:input-id` are bound to the same id.
import PvSelect from 'primevue/select'
import ToggleSwitch from 'primevue/toggleswitch'
import type { FieldValue, RecipeField } from './recipes/types'

const props = defineProps<{ field: RecipeField; cameras: string[] }>()
const model = defineModel<FieldValue>({ required: true })

const inputId = computed(() => `recipe-field-${props.field.key}`)

/** A camera-sourced field takes the live camera list; everything else uses
 * the options the recipe declared. */
const options = computed(() =>
  props.field.source === 'cameras'
    ? props.cameras.map((camera) => ({ label: camera, value: camera }))
    : (props.field.options ?? []),
)

// InputNumber rounds to whole numbers unless told otherwise, which would
// quietly turn a 0.05-step confidence into 0 or 1.
const fractionDigits = computed(() => ((props.field.step ?? 1) < 1 ? 2 : 0))

const numberModel = computed({
  get: () => (typeof model.value === 'number' ? model.value : Number(model.value) || 0),
  // InputNumber hands back null while the box is empty mid-edit; the
  // builders' own fallbacks cover that, so the preview keeps rendering.
  set: (value: number | null) => {
    model.value = value ?? 0
  },
})

const textModel = computed({
  get: () => (typeof model.value === 'string' ? model.value : ''),
  set: (value: string | null) => {
    model.value = value ?? ''
  },
})

const listModel = computed({
  get: () => (Array.isArray(model.value) ? model.value : []),
  set: (value: string[] | null) => {
    model.value = value ?? []
  },
})

const boolModel = computed({
  get: () => model.value === true,
  set: (value: boolean) => {
    model.value = value
  },
})
</script>

<template>
  <div class="recipe-field" :class="`recipe-field-${field.key}`">
    <label :for="inputId" class="field-label">{{ field.label }}</label>

    <div class="recipe-field-input">
      <InputNumber
        v-if="field.type === 'number'"
        v-model="numberModel"
        :input-id="inputId"
        :min="field.min"
        :max="field.max"
        :step="field.step ?? 1"
        :max-fraction-digits="fractionDigits"
        :suffix="field.suffix ? ` ${field.suffix}` : undefined"
        show-buttons
        button-layout="horizontal"
        class="recipe-number"
      />
      <ToggleSwitch
        v-else-if="field.type === 'toggle'"
        v-model="boolModel"
        :input-id="inputId"
        :aria-label="field.label"
      />
      <PvSelect
        v-else-if="field.type === 'select'"
        v-model="textModel"
        :input-id="inputId"
        :options="options"
        option-label="label"
        option-value="value"
        class="recipe-wide"
      />
      <MultiSelect
        v-else-if="field.type === 'multiselect'"
        v-model="listModel"
        :input-id="inputId"
        :options="options"
        option-label="label"
        option-value="value"
        :placeholder="field.placeholder ?? 'All'"
        display="chip"
        filter
        class="recipe-wide"
      />
      <InputMask
        v-else-if="field.type === 'time'"
        :id="inputId"
        v-model="textModel"
        mask="99:99"
        :placeholder="field.placeholder ?? '08:00'"
        class="recipe-wide"
      />
      <InputText v-else :id="inputId" v-model="textModel" :placeholder="field.placeholder" class="recipe-wide" />
    </div>

    <span v-if="field.help" class="recipe-field-help">{{ field.help }}</span>
  </div>
</template>

<style scoped>
.recipe-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  min-width: 0;
}
.recipe-field-help {
  font-size: 0.72rem;
  color: var(--muted);
  line-height: 1.45;
}
.recipe-field-input {
  display: flex;
  min-width: 0;
}
.recipe-wide {
  width: 100%;
}
.recipe-number {
  width: 100%;
}
/* Without this the inner input keeps its default size, so the component
   overflows its grid cell (and, with horizontal buttons, overlaps whatever
   is beside it). */
.recipe-number :deep(.p-inputnumber-input) {
  width: 100%;
  min-width: 0;
}
</style>
