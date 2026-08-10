<script setup lang="ts">
import AppIcon from '../icons/AppIcon.vue'
import { usePromptOverlayStore } from '../../stores/promptOverlay'

const store = usePromptOverlayStore()
</script>

<!-- z-index above the default 100 (matches TwoFAOverlay's own override):
     ClipModal is teleported to <body> (see LibraryPage.vue) so it stays
     visible across tab switches, which puts it *after* #app as a body
     sibling — at equal z-index it would always paint over this overlay,
     which opens *from inside* an already-open clip modal. A leading
     comment *inside* <template> makes Vue compile this as a multi-root
     fragment in dev/test mode, breaking wrapper.classes()/wrapper.element
     in tests — keep any template-level comments outside <template>. -->
<template>
  <div class="modal-bg nested-overlay" :class="{ open: store.open }" style="z-index: 150" @click.self="store.close()">
    <div class="modal" style="max-width: 700px">
      <button type="button" class="modal-close" title="Close (Esc)" @click="store.close()">
        <AppIcon name="close" />
      </button>
      <div class="modal-body">
        <div class="modal-title" style="margin-bottom: 0.7rem">📝 Prompt Sent to AI</div>
        <p style="font-size: 0.78rem; color: var(--muted); margin-bottom: 0.6rem">
          The exact text sent to the model for this clip (image frames are not shown).
        </p>
        <p v-if="!store.promptText" style="font-size: 0.85rem; color: var(--muted)">
          No prompt was recorded for this clip's analysis — prompt debug mode was off when it ran. It's on now, so
          re-analyzing this clip (⟳ Re-analyze, in the AI Analysis panel) will capture it.
        </p>
        <pre
          v-else
          style="
            font-size: 0.78rem;
            font-family: monospace;
            background: var(--card2);
            border: 1px solid var(--border);
            border-radius: var(--radius);
            padding: 0.75rem 0.9rem;
            white-space: pre-wrap;
            overflow-wrap: break-word;
            max-height: 60vh;
            overflow-y: auto;
            color: var(--text);
          "
          >{{ store.promptText }}</pre>
      </div>
    </div>
  </div>
</template>
