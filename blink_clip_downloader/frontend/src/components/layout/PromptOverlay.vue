<script setup lang="ts">
import AppIcon from '../icons/AppIcon.vue'
import { usePromptOverlayStore } from '../../stores/promptOverlay'

const store = usePromptOverlayStore()
</script>

<template>
  <div class="modal-bg" :class="{ open: store.open }" @click.self="store.close()">
    <div class="modal" style="max-width: 700px">
      <button class="modal-close" title="Close (Esc)" @click="store.close()">
        <AppIcon name="close" />
      </button>
      <div class="modal-body">
        <div class="modal-title" style="margin-bottom: 0.7rem">📝 Prompt Sent to AI</div>
        <p style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem">
          The exact text sent to the model for this clip (image frames are not shown).
        </p>
        <pre
          style="
            font-size: 0.78rem;
            font-family: monospace;
            background: var(--card2);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 0.75rem 0.9rem;
            white-space: pre-wrap;
            word-break: break-word;
            max-height: 60vh;
            overflow-y: auto;
            color: var(--text);
          "
          >{{ store.promptText }}</pre
        >
      </div>
    </div>
  </div>
</template>
