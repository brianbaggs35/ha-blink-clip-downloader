<script setup lang="ts">
import { useToastStore } from '../../stores/toast'

const props = defineProps<{ code: string }>()
const toast = useToastStore()

async function copy() {
  try {
    await navigator.clipboard.writeText(props.code)
    toast.show('Copied to clipboard')
  } catch {
    toast.show('Copy failed', true)
  }
}
</script>

<template>
  <div class="code-block"><button type="button" class="copy-btn" @click="copy">Copy</button>{{ code }}</div>
</template>
