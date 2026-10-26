import type {
  BackupPreview,
  RestoreClipBreakdown,
  RestoreReport,
  RestoreSummary,
  RestoreTableRow,
} from '../../api/types'

// Fixtures shared by the Backup & Restore specs. Not a spec itself — Vitest
// only collects *.spec.ts — and never imported by the app.

export function row(name: string, rows: number, added: number, label = name): RestoreTableRow {
  return { name, label, rows, added, skipped: rows - added }
}

export function breakdown(overrides: Partial<RestoreClipBreakdown> = {}): RestoreClipBreakdown {
  return { already_here: 0, same_file: 0, standins_replaced: 0, with_video: 0, without_video: 0, ...overrides }
}

export function summary(overrides: Partial<RestoreSummary> = {}): RestoreSummary {
  return {
    upload_id: 'u1',
    created_at: '2026-10-18T12:00:00+00:00',
    addon_version: '6.1.4',
    clip_folder: '/share/blink-clips',
    verified_parts: 14,
    tables: [
      row('clips', 120, 100, 'Clips'),
      row('analysis_results', 90, 80, 'AI analyses'),
      row('detected_objects', 0, 0),
    ],
    clips: breakdown({ already_here: 20, without_video: 100 }),
    files: [
      { label: 'Camera settings', status: 'new', detail: 'Not here yet; will be restored.' },
      { label: 'Protected assets', status: 'exists', detail: 'Already here; kept unless you choose to replace it.' },
    ],
    ...overrides,
  }
}

export function report(overrides: Partial<RestoreReport> = {}): RestoreReport {
  return {
    tables: [row('clips', 120, 100, 'Clips'), row('analysis_results', 90, 80, 'AI analyses')],
    clips: breakdown({ already_here: 20, without_video: 100 }),
    files: [{ label: 'Camera settings', status: 'restored', detail: 'Restored.' }],
    checks: [
      { label: 'Every record in the backup was restored or deliberately left alone', ok: true, detail: '' },
      { label: 'All 100 added clips are in the library', ok: true, detail: '' },
    ],
    verified: true,
    restart_needed: true,
    ...overrides,
  }
}

export function preview(overrides: Partial<BackupPreview> = {}): BackupPreview {
  return {
    tables: [
      { name: 'clips', label: 'Clips', rows: 1204, biometric: false },
      { name: 'analysis_results', label: 'AI analyses', rows: 980, biometric: false },
      { name: 'face_enrollments', label: 'Enrolled faces', rows: 3, biometric: true },
      { name: 'security_events', label: 'Security events', rows: 0, biometric: false },
    ],
    files: [
      { label: 'Camera settings', items: 1 },
      { label: 'Protected assets', items: 0 },
      { label: 'Asset reference pictures', items: 2 },
    ],
    clip_folder: '/share/blink-clips',
    gdrive: { state: 'not_connected', folder: '' },
    ...overrides,
  }
}
