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
      // PrimeVue's own ToastMessage template always renders the `summary`
      // <span> (no v-if), but only renders `detail` when it's set (v-if).
      // We only ever have one line of text — putting it in `detail` (as
      // this used to) left an empty-but-still-laid-out summary line above
      // it, which visually pushed the message down and threw off the
      // icon's vertical alignment against it. `summary` is the field this
      // component actually expects to always be populated.
      summary: store.message,
      life: store.duration,
    })
  },
)
</script>

<template>
  <Toast position="bottom-right" />
</template>
