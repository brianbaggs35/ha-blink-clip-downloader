import { defineStore } from 'pinia'

/** Cross-cutting hardware-capability flags, fetched once by AppSidebar (see
 *  its connection-badge polling for the same pattern) since they gate UI
 *  that lives outside any single tab — e.g. whether the Biometrics tab
 *  should even be shown. `null` means "not checked yet", distinct from
 *  `false` ("checked, unavailable") so nothing is hidden prematurely while
 *  the initial fetch is still in flight. */
export const useCapabilitiesStore = defineStore('capabilities', {
  state: () => ({
    /** Mirrors GET /api/ai/faces' `available` field (vision.py's
     *  is_face_recognition_available()) — false if the facenet_pytorch
     *  package is missing *or* the CPU can't safely run it (e.g. a
     *  Raspberry Pi 4's Cortex-A72). Deliberately read from the faces
     *  endpoint rather than /api/ai/status, since that field is only
     *  present when AI analysis itself is enabled, while someone may want
     *  to manage face enrollments with analysis off. */
    faceRecognitionAvailable: null as boolean | null,
  }),
  actions: {
    setFaceRecognitionAvailable(value: boolean) {
      this.faceRecognitionAvailable = value
    },
  },
})
