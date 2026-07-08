import { defineStore } from 'pinia'

let hideTimer: ReturnType<typeof setTimeout> | undefined

export const useToastStore = defineStore('toast', {
  state: () => ({
    message: '',
    isError: false,
    visible: false,
  }),
  actions: {
    show(message: string, isError = false, duration = 2800) {
      this.message = message
      this.isError = isError
      this.visible = true
      clearTimeout(hideTimer)
      hideTimer = setTimeout(() => {
        this.visible = false
      }, duration)
    },
  },
})
