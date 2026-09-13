<script setup lang="ts">
import { computed } from 'vue'
import Checkbox from 'primevue/checkbox'
import IconField from 'primevue/iconfield'
import InputIcon from 'primevue/inputicon'
import InputText from 'primevue/inputtext'
import Select from 'primevue/select'
import AppIcon from '../icons/AppIcon.vue'

// Shared by both of LibraryPage's two filter-row renders (see its own
// comment on .lib-filters-desktop/.lib-filters-mobile): desktop always
// shows a plain, always-expanded row; mobile wraps the same fields in a
// collapsible Panel to save vertical space. Both renders bind to the same
// parent refs via these models, so they always stay in sync even though
// only one is ever visible per viewport — idSuffix keeps their form-field
// ids/labels from colliding since both copies exist in the DOM at once
// (CSS media queries decide which one is visible, not v-if/v-else, so
// there is no JS viewport-detection logic to test or get wrong).
const props = withDefaults(defineProps<{ tags: string[]; showRecognized: boolean; idSuffix?: string }>(), {
  idSuffix: '',
})

const search = defineModel<string>('search', { required: true })
const dateRange = defineModel<string>('dateRange', { required: true })
const sourceFilter = defineModel<string>('sourceFilter', { required: true })
const tagFilter = defineModel<string>('tagFilter', { required: true })
const sortOrder = defineModel<'newest' | 'oldest' | 'camera' | 'size' | 'duration'>('sortOrder', { required: true })
const starredOnly = defineModel<boolean>('starredOnly', { required: true })
const notifiedOnly = defineModel<boolean>('notifiedOnly', { required: true })
const recognizedOnly = defineModel<boolean>('recognizedOnly', { required: true })

const DATE_RANGE_OPTIONS = [
  { label: 'All time', value: '' },
  { label: 'Today', value: 'today' },
  { label: 'Yesterday', value: 'yesterday' },
  { label: 'This week', value: 'week' },
  { label: 'This month', value: 'month' },
]
const SOURCE_OPTIONS = [
  { label: 'All sources', value: '' },
  { label: 'Motion (PIR)', value: 'pir' },
  { label: 'Liveview', value: 'liveview' },
  { label: 'Snapshot', value: 'snapshot' },
  { label: 'Local Storage', value: 'local_storage' },
]
const SORT_OPTIONS = [
  { label: '⬆ Newest', value: 'newest' },
  { label: '⬇ Oldest', value: 'oldest' },
  { label: '📷 Camera', value: 'camera' },
  { label: '💾 Size', value: 'size' },
  { label: '⏱ Duration', value: 'duration' },
]

const tagOptions = computed(() => [
  { label: 'All tags', value: '' },
  ...props.tags.map((t) => ({ label: `#${t}`, value: t })),
])

function id(base: string): string {
  return `${base}${props.idSuffix}`
}
</script>

<template>
  <IconField class="lib-search">
    <InputIcon><AppIcon name="tab-library" style="width: 15px; height: 15px" /></InputIcon>
    <label :for="id('search')" class="sr-only">Search clips</label>
    <InputText :id="id('search')" v-model="search" size="small" placeholder="Search clips…" fluid />
  </IconField>
  <label :for="id('date-range')" class="sr-only">Date range</label>
  <Select
    :id="id('date-range')"
    v-model="dateRange"
    size="small"
    :options="DATE_RANGE_OPTIONS"
    option-label="label"
    option-value="value"
  />
  <label :for="id('source-filter')" class="sr-only">Source</label>
  <Select
    :id="id('source-filter')"
    v-model="sourceFilter"
    size="small"
    :options="SOURCE_OPTIONS"
    option-label="label"
    option-value="value"
    placeholder="All sources"
  />
  <label :for="id('tag-filter')" class="sr-only">Tag</label>
  <Select
    :id="id('tag-filter')"
    v-model="tagFilter"
    size="small"
    :options="tagOptions"
    option-label="label"
    option-value="value"
    placeholder="All tags"
  />
  <label :for="id('sort-order')" class="sr-only">Sort order</label>
  <Select
    :id="id('sort-order')"
    v-model="sortOrder"
    size="small"
    :options="SORT_OPTIONS"
    option-label="label"
    option-value="value"
  />
  <label :for="id('lib-filter-starred')" class="lib-check">
    <Checkbox v-model="starredOnly" :input-id="id('lib-filter-starred')" binary /> ★ Starred
  </label>
  <label :for="id('lib-filter-notified')" class="lib-check">
    <Checkbox v-model="notifiedOnly" :input-id="id('lib-filter-notified')" binary /> 🔔 Notified
  </label>
  <label v-if="showRecognized" :for="id('lib-filter-recognized')" class="lib-check">
    <Checkbox v-model="recognizedOnly" :input-id="id('lib-filter-recognized')" binary /> 👤 Recognized
  </label>
</template>
