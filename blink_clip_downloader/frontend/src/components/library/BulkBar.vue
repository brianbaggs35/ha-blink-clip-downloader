<script setup lang="ts">
defineProps<{ count: number; zipping: boolean }>()
defineEmits<{ star: []; delete: []; zip: []; cancel: [] }>()
</script>

<template>
  <div id="bulk-bar" class="bulk-bar">
    <span id="sel-count">{{ count }} selected</span>
    <button class="btn sm" @click="$emit('star')">★ Star all</button>
    <button class="btn sm" @click="$emit('delete')">🗑 Delete all</button>
    <button class="btn sm" :disabled="zipping" @click="$emit('zip')">
      {{ zipping ? '⏳ Zipping…' : '⬇ ZIP' }}
    </button>
    <button class="btn sm" style="margin-left: auto" @click="$emit('cancel')">✕ Cancel</button>
  </div>
</template>
