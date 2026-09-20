<script setup lang="ts">
import { useToastStore } from '../../stores/toast'

const props = withDefaults(
  defineProps<{
    code: string
    /** Offering a filename adds a Download button. Long generated YAML is
     * awkward to move by clipboard alone — a blueprint in particular has to
     * end up as a file on disk anyway. */
    filename?: string
  }>(),
  { filename: '' },
)
const toast = useToastStore()

async function copy() {
  try {
    await navigator.clipboard.writeText(props.code)
    toast.show('Copied to clipboard')
  } catch {
    toast.show('Copy failed', true)
  }
}

function download() {
  try {
    const url = URL.createObjectURL(new Blob([props.code], { type: 'text/yaml' }))
    const link = document.createElement('a')
    link.href = url
    link.download = props.filename
    link.click()
    URL.revokeObjectURL(url)
    toast.show(`Downloaded ${link.download}`)
  } catch {
    toast.show('Download failed', true)
  }
}
</script>

<template>
  <div class="code-block">
    <div class="code-block-actions">
      <button type="button" class="copy-btn" @click="copy">Copy</button>
      <button v-if="filename" type="button" class="copy-btn" @click="download">Download</button>
    </div>
    <!-- The code must be its own element, not a bare interpolation: this
         block renders with `white-space: pre`, and a text node next to a
         sibling element keeps the template's own indentation, which shows
         up as a leading space on the first line — significant, if someone
         selects the YAML rather than pressing Copy. -->
    <span class="code-block-text">{{ code }}</span>
  </div>
</template>

<style scoped>
.code-block-actions {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  display: flex;
  gap: 0.35rem;
}
.code-block-actions .copy-btn {
  position: static;
}
</style>
