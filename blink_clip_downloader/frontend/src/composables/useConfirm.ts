import { useConfirmStore } from '../stores/confirm'

/** `if (await confirm('Delete this clip?')) { ... }` — replaces native `window.confirm()`. */
export function useConfirm() {
  const store = useConfirmStore()
  return (message: string, title?: string) => store.ask(message, title)
}
