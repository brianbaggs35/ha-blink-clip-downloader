import { defineStore } from 'pinia'
import type { CameraStat } from '../api/types'

/** Camera list + selected-camera filter, shared between LibraryPage (which
 *  owns loading it and reacting to selection) and AppSidebar (which renders
 *  the camera nav inside the main sidebar instead of a second column, so it
 *  only needs to exist once regardless of which tab is active). */
export const useLibraryStore = defineStore('library', {
  state: () => ({
    cameras: [] as CameraStat[],
    currentCamera: 'all',
  }),
  actions: {
    setCameras(cameras: CameraStat[]) {
      this.cameras = cameras
    },
    selectCamera(camera: string) {
      this.currentCamera = camera
    },
  },
})
