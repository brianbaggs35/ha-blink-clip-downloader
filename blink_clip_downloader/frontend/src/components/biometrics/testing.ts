import type { ClipListItem, FaceCandidate, FaceEnrollment } from '../../api/types'

// Fixtures shared by the Biometrics specs. Not a spec itself — Vitest only
// collects *.spec.ts — and never imported by the app.

export function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
  } as Response
}

/** A failed response whose body is *body* — JSON text or a plain reason. */
export function errorResponse(status: number, body = ''): Response {
  return {
    ok: false,
    status,
    statusText: 'Error',
    text: () => Promise.resolve(body),
  } as Response
}

export function clip(id: string, overrides: Partial<ClipListItem> = {}): ClipListItem {
  return {
    id,
    camera: 'Front Door',
    timestamp: '2026-01-01T10:00:00Z',
    source: 'pir',
    file_path: `/clips/${id}.mp4`,
    size_bytes: 1,
    duration: 12,
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '',
    archived: false,
    archive_path: '',
    gdrive_backed_up: false,
    gdrive_file_id: '',
    gdrive_uploaded_at: '',
    notified: false,
    face_recognized: false,
    ...overrides,
  }
}

export function candidate(id: string, overrides: Partial<FaceCandidate> = {}): FaceCandidate {
  return {
    id,
    thumbnail: `data:image/jpeg;base64,${id}`,
    quality: 0.8,
    width: 60,
    time: 1.5,
    match: null,
    ...overrides,
  }
}

export function enrollment(overrides: Partial<FaceEnrollment> = {}): FaceEnrollment {
  return {
    id: 1,
    name: 'Brian',
    created_at: '2026-01-01T00:00:00Z',
    approved: true,
    has_thumbnail: true,
    frame_width: 640,
    camera: 'Front Door',
    warning: null,
    ...overrides,
  }
}

/** A promise the test resolves by hand, to hold a request in flight. */
export function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}
