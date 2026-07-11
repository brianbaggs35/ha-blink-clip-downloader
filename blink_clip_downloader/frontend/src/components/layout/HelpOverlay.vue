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

<template>
  <div class="modal-bg" :class="{ open }" @click.self="open = false">
    <div class="modal" style="max-width: 460px">
      <button class="modal-close" title="Close (Esc)" @click="open = false">
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
