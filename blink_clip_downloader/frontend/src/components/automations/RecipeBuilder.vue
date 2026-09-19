<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import Button from 'primevue/button'
import Listbox from 'primevue/listbox'
import Message from 'primevue/message'
import Tag from 'primevue/tag'
import CodeBlock from './CodeBlock.vue'
import RecipeFieldInput from './RecipeFieldInput.vue'
import { type Recipe, type RecipeValues, defaultValues } from './recipes/types'
import { readLocal, writeLocal } from '../../localStorage'

const props = defineProps<{
  recipes: Recipe[]
  cameras: string[]
  /** Remembers which recipe was last open, per builder. */
  storageKey: string
}>()

/** Where each kind of output belongs, phrased as the click path rather than
 * just a filename — the file is the part people already know. */
const TARGET_HINTS: Record<string, string> = {
  'automations.yaml':
    'Settings → Automations & scenes → Create automation → Create new automation, then use the three-dot menu → Edit in YAML and paste over what is there.',
  'scripts.yaml':
    'Settings → Automations & scenes → Scripts → Add script, then three-dot menu → Edit in YAML. The first line is the script id, so paste the whole block into scripts.yaml if you keep scripts in a file.',
  'scenes.yaml': 'Paste into scenes.yaml, then Developer tools → YAML → Reload scenes.',
  'configuration.yaml':
    'Paste into configuration.yaml (or a file it includes), then Developer tools → YAML → check the configuration and reload.',
}

const grouped = computed(() => {
  const groups: { label: string; items: Recipe[] }[] = []
  for (const recipe of props.recipes) {
    const existing = groups.find((group) => group.label === recipe.group)
    if (existing) existing.items.push(recipe)
    else groups.push({ label: recipe.group, items: [recipe] })
  }
  return groups
})

// The catalogues are module constants, so there is always a first recipe to
// fall back to — including when a remembered id names one that a later
// version of the add-on removed.
const selectedId = ref(readLocal(props.storageKey) || props.recipes[0].id)
const selected = computed(() => props.recipes.find((recipe) => recipe.id === selectedId.value) ?? props.recipes[0])

const values = ref<RecipeValues>(defaultValues(selected.value))

watch(selectedId, (id) => {
  if (!id) return
  writeLocal(props.storageKey, id)
  values.value = defaultValues(selected.value)
})

function reset() {
  values.value = defaultValues(selected.value)
}

const yaml = computed(() => {
  try {
    return selected.value.build(values.value)
  } catch {
    // A half-typed field should never blank the page. The builders are pure
    // string assembly, so this is a backstop, not an expected path.
    return '# Could not generate this one — check the values above.'
  }
})

const hint = computed(() => TARGET_HINTS[selected.value.target] ?? '')
</script>

<template>
  <div class="recipe-builder">
    <Listbox
      v-model="selectedId"
      :options="grouped"
      option-label="name"
      option-value="id"
      option-group-label="label"
      option-group-children="items"
      filter
      filter-placeholder="Search"
      scroll-height="30rem"
      class="recipe-listbox"
      :pt="{ list: { 'aria-label': 'Available automations' } }"
    >
      <template #option="{ option }">
        <span class="recipe-option">
          <span class="recipe-option-icon" aria-hidden="true">{{ option.icon }}</span>
          <span>{{ option.name }}</span>
        </span>
      </template>
    </Listbox>

    <div class="recipe-main">
      <div class="recipe-head">
        <h3 class="recipe-title">
          <span aria-hidden="true">{{ selected.icon }}</span> {{ selected.name }}
        </h3>
        <Tag :value="selected.target" severity="secondary" />
      </div>
      <p class="recipe-desc">{{ selected.description }}</p>

      <div class="recipe-fields">
        <RecipeFieldInput
          v-for="field in selected.fields"
          :key="field.key"
          v-model="values[field.key]"
          :field="field"
          :cameras="cameras"
        />
      </div>

      <Message
        v-if="!cameras.length && selected.fields.some((f) => f.source === 'cameras')"
        severity="info"
        size="small"
        :closable="false"
        class="recipe-note"
      >
        No cameras to choose from yet — the generated YAML will apply to every camera, which is usually what you want
        anyway.
      </Message>

      <div class="recipe-actions">
        <Button size="small" outlined severity="secondary" @click="reset">Reset to defaults</Button>
      </div>

      <CodeBlock :code="yaml" :filename="selected.filename" />
      <p v-if="hint" class="recipe-hint">{{ hint }}</p>
    </div>
  </div>
</template>

<style scoped>
.recipe-builder {
  display: grid;
  /* min(...) so the picker column collapses instead of overflowing a phone
     viewport — same rule the rest of the app's grids follow. */
  grid-template-columns: minmax(min(240px, 100%), 240px) minmax(0, 1fr);
  gap: 1.1rem;
  align-items: start;
}
@media (max-width: 720px) {
  .recipe-builder {
    grid-template-columns: minmax(0, 1fr);
  }
}
.recipe-listbox {
  width: 100%;
}
.recipe-option {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  min-width: 0;
}
.recipe-option-icon {
  font-size: 0.95rem;
}
.recipe-main {
  min-width: 0;
}
.recipe-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.5rem;
}
.recipe-title {
  margin: 0;
  font-size: 0.95rem;
  font-weight: 700;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 0.45rem;
}
.recipe-desc {
  margin: 0.35rem 0 0.9rem;
  font-size: 0.83rem;
  color: var(--muted);
  line-height: 1.55;
}
.recipe-fields {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(240px, 100%), 1fr));
  gap: 0.9rem 1rem;
  margin-bottom: 0.9rem;
}
.recipe-note {
  margin-bottom: 0.8rem;
}
.recipe-actions {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 0.5rem;
}
.recipe-hint {
  font-size: 0.75rem;
  color: var(--muted);
  line-height: 1.5;
  margin: -0.6rem 0 0;
}
</style>
