import { defineStore } from 'pinia'

/** Backs the shared ConfirmDialog — the replacement for native `window.confirm()`
 *  used across the app for destructive actions (delete clip, bulk delete, ...). */
export const useConfirmStore = defineStore('confirm', {
  state: () => ({
    open: false,
    title: 'Are you sure?',
    message: '',
    resolve: null as ((result: boolean) => void) | null,
  }),
  actions: {
    ask(message: string, title = 'Are you sure?'): Promise<boolean> {
      this.title = title
      this.message = message
      this.open = true
      return new Promise((resolve) => {
        this.resolve = resolve
      })
    },
    settle(result: boolean) {
      this.open = false
      this.resolve?.(result)
      this.resolve = null
    },
  },
})
