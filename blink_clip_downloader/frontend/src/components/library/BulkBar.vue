<script setup lang="ts">
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
    <button v-if="count < total" type="button" class="btn sm" @click="$emit('selectAll')">
      Select all {{ total }}
    </button>
    <button type="button" class="btn sm" @click="$emit('star')">★ Star selected</button>
    <button type="button" class="btn sm" @click="$emit('delete')">🗑 Delete selected</button>
    <button type="button" class="btn sm" :disabled="zipping" @click="$emit('zip')">
      {{ zipping ? '⏳ Zipping…' : '⬇ ZIP' }}
    </button>
    <button v-if="aiEnabled" type="button" class="btn sm" :disabled="analyzing" @click="$emit('analyze')">
      {{ analyzing ? '⏳ Analyzing…' : '🔬 Analyze selected' }}
    </button>
    <button v-if="gdriveEnabled" type="button" class="btn sm" @click="$emit('upload')">☁ Upload to Drive</button>
    <button type="button" class="btn sm" style="margin-left: auto" @click="$emit('cancel')">✕ Cancel</button>
  </div>
</template>
