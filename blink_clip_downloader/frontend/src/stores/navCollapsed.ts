import { defineStore } from 'pinia'

const STORAGE_KEY = 'blink_nav_collapsed'

// Expanded is the default — critical for Playwright specs, all of which
// click sidebar tabs assuming today's expanded layout with no opt-in
// needed. Only an explicit stored '1' collapses it on load.
function initialCollapsed(): boolean {
  return localStorage.getItem(STORAGE_KEY) === '1'
}

export const useNavCollapsedStore = defineStore('navCollapsed', {
  state: () => ({
    collapsed: initialCollapsed(),
  }),
  actions: {
    toggle() {
      this.collapsed = !this.collapsed
      localStorage.setItem(STORAGE_KEY, this.collapsed ? '1' : '0')
    },
  },
})
