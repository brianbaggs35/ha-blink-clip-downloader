<script setup lang="ts">
import { watch } from 'vue'
import Toast from 'primevue/toast'
import { useToast } from 'primevue/usetoast'
import { useToastStore } from '../../stores/toast'

const store = useToastStore()
const primeToast = useToast()

// Bridges the plain-state toast store (still the single call site every
// action uses: `useToastStore().show(message, isError)`) to PrimeVue's
// Toast — keeps every existing call site unchanged while upgrading the
// visual presentation to a PrimeVue component.
watch(
  () => store.seq,
  () => {
    if (!store.visible) return
    primeToast.add({
      severity: store.isError ? 'error' : 'success',
      detail: store.message,
      life: store.duration,
    })
  },
)
</script>

<template>
  <Toast position="bottom-right" />
</template>
