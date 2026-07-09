import { defineStore } from 'pinia'

/** Backs the shared PromptOverlay — shows the exact text sent to the AI
 *  provider for a clip (only reachable when prompt-debug is enabled).
 *  Opened from ClipAiPanel, which is itself shared between the Library
 *  modal and the AI tab's suspicious-activity feed. */
export const usePromptOverlayStore = defineStore('promptOverlay', {
  state: () => ({
    open: false,
    promptText: '',
  }),
  actions: {
    show(promptText: string) {
      this.promptText = promptText || 'No prompt was captured for this clip.'
      this.open = true
    },
    close() {
      this.open = false
    },
  },
})
