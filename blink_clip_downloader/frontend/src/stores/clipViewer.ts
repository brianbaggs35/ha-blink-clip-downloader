import { defineStore } from 'pinia'

/** Cross-tab bridge for opening the Library tab's clip modal from elsewhere
 *  (e.g. the AI tab's suspicious-activity feed, or the Security tab's
 *  timeline) without switching tabs — mirrors the pre-Vue UI, where the
 *  modal lived outside every `.page` div so it stayed visible no matter
 *  which tab was active. LibraryPage owns the actual modal/clip-list logic
 *  and watches `seq` to open `clipId`. */
export const useClipViewerStore = defineStore('clipViewer', {
  state: () => ({
    clipId: null as string | null,
    /** Clip-relative seconds to start playback at, when the caller knows
     *  which moment matters — the Security tab opens a clip at the second
     *  its event was measured at, so reviewing "possible contact at 0:06"
     *  does not mean scrubbing for it by hand. Null means "from the top". */
    startAt: null as number | null,
    seq: 0,
  }),
  actions: {
    requestOpen(clipId: string, startAt: number | null = null) {
      this.clipId = clipId
      this.startAt = startAt
      this.seq++
    },
  },
})
