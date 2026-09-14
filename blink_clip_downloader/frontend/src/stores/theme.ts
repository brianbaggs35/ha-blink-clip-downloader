import { readLocal, writeLocal } from '../localStorage'
import { defineStore } from 'pinia'

const STORAGE_KEY = 'blink_theme'

// Dark is the shipped default regardless of OS/browser preference (since
// v4.0.0) — an explicit stored choice (either way) is always honored, but
// unset means dark, not a prefers-color-scheme lookup.
function initialIsDark(): boolean {
  return readLocal(STORAGE_KEY) !== 'light'
}

export const useThemeStore = defineStore('theme', {
  state: () => ({
    isDark: initialIsDark(),
  }),
  actions: {
    toggle() {
      this.isDark = !this.isDark
      writeLocal(STORAGE_KEY, this.isDark ? 'dark' : 'light')
    },
  },
})
