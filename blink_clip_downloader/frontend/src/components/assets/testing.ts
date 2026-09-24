// Fixtures shared by the Assets tab's specs.
import type { AssetActivity, ClipListItem, ProtectedAsset } from '../../api/types'

export function asset(overrides: Partial<ProtectedAsset> = {}): ProtectedAsset {
  return {
    id: 'door1',
    camera: 'Porch',
    name: 'Front door',
    asset_type: 'door',
    description: '',
    zone: { shape: 'rect', x_min: 0.1, y_min: 0.2, x_max: 0.3, y_max: 0.8 },
    enabled: true,
    created_at: '2026-09-24T10:00:00+00:00',
    updated_at: '2026-09-24T10:00:00+00:00',
    ...overrides,
  }
}

export function activity(overrides: Partial<AssetActivity> = {}): AssetActivity {
  return {
    camera: 'Porch',
    asset_name: 'Front door',
    clips: 3,
    last_seen: '2026-09-24T09:00:00+00:00',
    top_severity: 'suspicious',
    ...overrides,
  }
}

export function clip(id: string, camera = 'Porch'): ClipListItem {
  return {
    id,
    camera,
    file_path: `/data/${id}.mp4`,
    timestamp: '2026-09-24T10:00:00Z',
    size_bytes: 1000,
    duration: 5,
    source: 'pir',
    network_id: 1,
    starred: false,
    tags: [],
    downloaded_at: '2026-09-24T10:01:00Z',
    archived: false,
    archive_path: '',
    gdrive_backed_up: false,
    gdrive_file_id: '',
    gdrive_uploaded_at: '',
    notified: false,
    face_recognized: false,
  }
}

export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    statusText: 'x',
    json: () => Promise.resolve(body),
    // Only an error response's body is read as text, and those are plain
    // strings, as aiohttp's HTTPException(text=...) sends them.
    text: () => Promise.resolve(String(body)),
    headers: new Headers(),
  } as Response
}
