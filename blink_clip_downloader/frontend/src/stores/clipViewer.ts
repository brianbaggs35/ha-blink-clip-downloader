import { defineStore } from 'pinia'

/** Cross-tab bridge for opening the Library tab's clip modal from elsewhere
 *  (e.g. the AI tab's suspicious-activity feed) without switching tabs —
 *  mirrors the pre-Vue UI, where the modal lived outside every `.page` div
 *  so it stayed visible no matter which tab was active. LibraryPage owns
 *  the actual modal/clip-list logic and watches `seq` to open `clipId`. */
export const useClipViewerStore = defineStore('clipViewer', {
  state: () => ({
    clipId: null as string | null,
    seq: 0,
  }),
  actions: {
    requestOpen(clipId: string) {
      this.clipId = clipId
      this.seq++
    },
  },
})
