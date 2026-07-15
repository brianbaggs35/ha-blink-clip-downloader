<script setup lang="ts">
import ProgressSpinner from 'primevue/progressspinner'

withDefaults(defineProps<{ label?: string }>(), { label: 'Loading…' })
</script>

<template>
  <div class="loading-indicator">
    <ProgressSpinner style="width: 2.5rem; height: 2.5rem" stroke-width="4" />
    <span class="loading-indicator-label">{{ label }}</span>
  </div>
</template>

<style scoped>
.loading-indicator {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.6rem;
  color: var(--muted);
}
.loading-indicator-label {
  font-size: 0.85rem;
}
</style>
