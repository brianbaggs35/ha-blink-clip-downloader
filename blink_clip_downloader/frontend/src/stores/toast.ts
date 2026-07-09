import { defineStore } from 'pinia'

let hideTimer: ReturnType<typeof setTimeout> | undefined

export const useToastStore = defineStore('toast', {
  state: () => ({
    message: '',
    isError: false,
    visible: false,
    duration: 2800,
    // Bumped on every show() so ToastHost's watcher can tell two calls
    // apart even when they share the same message/isError (Vue's watch
    // only fires on an actual value change otherwise).
    seq: 0,
  }),
  actions: {
    show(message: string, isError = false, duration = 2800) {
      this.message = message
      this.isError = isError
      this.visible = true
      this.duration = duration
      this.seq++
      clearTimeout(hideTimer)
      hideTimer = setTimeout(() => {
        this.visible = false
      }, duration)
    },
  },
})
