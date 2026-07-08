import { defineStore } from 'pinia'

/** Bumped by the sidebar's Refresh button (immediately) and Sync button
 *  (~10s after triggering a download, giving new clips time to land) — tab
 *  components watch `tick` and reload their own data, mirroring the
 *  pre-Vue UI's shared `loadAll()`. */
export const useRefreshStore = defineStore('refresh', {
  state: () => ({ tick: 0 }),
  actions: {
    bump() {
      this.tick++
    },
  },
})
