import { defineStore } from 'pinia'

/** Backs the sidebar's connection badge — updated by whichever tab last
 *  fetched `/api/stats` (Library, Status), since Blink connectivity is a
 *  cross-cutting concern surfaced in the shell, not owned by one tab. */
export const useConnectionStore = defineStore('connection', {
  state: () => ({
    connected: null as boolean | null,
  }),
  actions: {
    setConnected(value: boolean) {
      this.connected = value
    },
  },
})
