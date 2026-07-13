import { onMounted, onUnmounted, type Ref } from 'vue'
import { useConfirmStore } from '../stores/confirm'
import { usePromptOverlayStore } from '../stores/promptOverlay'

/** Global shell-level shortcuts: `?` toggles the help overlay, `Esc` closes
 *  whichever overlay is topmost (help, then the AI prompt-debug overlay,
 *  then the confirm dialog).
 *
 *  Video-player shortcuts (Space/arrows/F/M/L) and the clip modal's own Esc
 *  handling are scoped to the Library tab's ClipModal instead of living
 *  here — each modal owns its own keydown listener rather than one growing
 *  global switch statement. ClipModal defers its own Esc-closes-me handling
 *  when the prompt overlay or confirm dialog is open on top of it, so the
 *  priority chain stays correct across both listeners. */
export function useKeyboardShortcuts(helpOpen: Ref<boolean>) {
  const confirm = useConfirmStore()
  const promptOverlay = usePromptOverlayStore()

  function onKeydown(e: KeyboardEvent) {
    const target = e.target as HTMLElement
    // Any text-entry surface must be excluded, not just <input> — a Textarea
    // (e.g. the Vehicles tab's Protected Vehicle Description field, or the
    // AI tab's custom-prompt field) or a <select> receiving a stray '?'/Esc
    // keystroke would otherwise toggle the global help overlay or close a
    // dialog mid-keystroke.
    if (
      target.tagName === 'INPUT' ||
      target.tagName === 'TEXTAREA' ||
      target.tagName === 'SELECT' ||
      target.isContentEditable
    ) {
      return
    }

    if (e.key === '?') {
      helpOpen.value = !helpOpen.value
      return
    }
    if (e.key === 'Escape') {
      if (helpOpen.value) {
        helpOpen.value = false
        return
      }
      if (promptOverlay.open) {
        promptOverlay.close()
        return
      }
      if (confirm.open) {
        confirm.settle(false)
        return
      }
    }
  }

  onMounted(() => document.addEventListener('keydown', onKeydown))
  onUnmounted(() => document.removeEventListener('keydown', onKeydown))
}
