import { onMounted, onUnmounted, type Ref } from 'vue'
import { useConfirmStore } from '../stores/confirm'

/** Global shell-level shortcuts: `?` toggles the help overlay, `Esc` closes
 *  whichever overlay is topmost (help, then the confirm dialog).
 *
 *  Video-player shortcuts (Space/arrows/F/M/L) and the prompt-debug overlay's
 *  own Esc handling are scoped to the Library tab's player modal instead of
 *  living here — each modal owns its own keydown listener rather than one
 *  growing global switch statement, added when that tab is ported. */
export function useKeyboardShortcuts(helpOpen: Ref<boolean>) {
  const confirm = useConfirmStore()

  function onKeydown(e: KeyboardEvent) {
    const target = e.target as HTMLElement
    if (target.tagName === 'INPUT') return

    if (e.key === '?') {
      helpOpen.value = !helpOpen.value
      return
    }
    if (e.key === 'Escape') {
      if (helpOpen.value) {
        helpOpen.value = false
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
