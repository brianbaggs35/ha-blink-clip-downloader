<script setup lang="ts">
import AppIcon from '../icons/AppIcon.vue'

const open = defineModel<boolean>({ required: true })

const SHORTCUTS: [string, string][] = [
  ['Space', 'Play / pause'],
  ['← →', 'Seek ±10 s'],
  ['↑ ↓', 'Previous / next clip'],
  ['F', 'Toggle fullscreen'],
  ['M', 'Toggle mute'],
  ['L', 'Toggle loop'],
  ['Esc', 'Close player or this overlay'],
  ['?', 'Show / hide this overlay'],
]
</script>

<!-- z-index above the default 100 — same reasoning as PromptOverlay.vue:
     this can be opened (via the ? shortcut) while a clip's modal, which is
     teleported to <body>, is already up. A leading comment *inside*
     <template> makes Vue compile this as a multi-root fragment in
     dev/test mode, breaking wrapper.classes()/wrapper.element in tests —
     keep any template-level comments outside the <template> tag. -->
<template>
  <div class="modal-bg nested-overlay" :class="{ open }" style="z-index: 150" @click.self="open = false">
    <div class="modal" style="max-width: 460px">
      <button type="button" class="modal-close" title="Close (Esc)" @click="open = false">
        <AppIcon name="close" />
      </button>
      <div class="modal-body">
        <div class="modal-title" style="margin-bottom: 0.9rem">⌨ Keyboard Shortcuts</div>
        <table style="width: 100%; border-collapse: collapse; font-size: 0.83rem">
          <thead>
            <tr>
              <th scope="col" class="sr-only">Key</th>
              <th scope="col" class="sr-only">Action</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="([key, desc], i) in SHORTCUTS"
              :key="i"
              :style="i < SHORTCUTS.length - 1 ? 'border-bottom: 1px solid var(--border)' : ''"
            >
              <td style="padding: 0.32rem 0.5rem; color: var(--muted)">
                <span class="kbd">{{ key }}</span>
              </td>
              <td style="padding: 0.32rem 0.5rem">{{ desc }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
