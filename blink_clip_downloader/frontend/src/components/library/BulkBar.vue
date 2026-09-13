<script setup lang="ts">
import Button from 'primevue/button'

defineProps<{
  count: number
  total: number
  zipping: boolean
  analyzing: boolean
  aiEnabled: boolean
  gdriveEnabled: boolean
}>()
defineEmits<{ star: []; delete: []; zip: []; analyze: []; upload: []; cancel: []; selectAll: [] }>()
</script>

<template>
  <div id="bulk-bar" class="bulk-bar">
    <span id="sel-count">{{ count }} selected</span>
    <!-- Selecting a clip's own checkbox one at a time was the only way to
         populate a selection — "star/delete all" then only ever meant "all
         [of what you individually checked]", which reads as broken when
         count is 0. This gives "all" something real to mean: every clip
         currently loaded in the grid (i.e. matching the active filters). -->
    <Button v-if="count < total" size="small" @click="$emit('selectAll')"> Select all {{ total }} </Button>
    <Button size="small" @click="$emit('star')">★ Star selected</Button>
    <Button size="small" @click="$emit('delete')">🗑 Delete selected</Button>
    <Button size="small" :disabled="zipping" @click="$emit('zip')">
      {{ zipping ? '⏳ Zipping…' : '⬇ ZIP' }}
    </Button>
    <Button v-if="aiEnabled" size="small" :disabled="analyzing" @click="$emit('analyze')">
      {{ analyzing ? '⏳ Analyzing…' : '🔬 Analyze selected' }}
    </Button>
    <Button v-if="gdriveEnabled" size="small" @click="$emit('upload')">☁ Upload to Drive</Button>
    <Button size="small" style="margin-left: auto" @click="$emit('cancel')">✕ Cancel</Button>
  </div>
</template>
