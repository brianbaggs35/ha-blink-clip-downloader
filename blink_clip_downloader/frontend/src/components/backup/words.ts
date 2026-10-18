import type { RestoreClipBreakdown, RestoreFileStatus, RestoreTableRow } from '../../api/types'

// The sentences the summary and the report share. Written once because what
// the page promises before a restore and what it reports after have to say
// the same thing about the same numbers.

export function plural(count: number, word: string): string {
  return `${count.toLocaleString()} ${word}${count === 1 ? '' : 's'}`
}

/** The tables worth a row: one with nothing in the backup has nothing to say. */
export function nonEmpty(tables: RestoreTableRow[]): RestoreTableRow[] {
  return tables.filter((table) => table.rows > 0)
}

export function totalAdded(tables: RestoreTableRow[]): number {
  return tables.reduce((sum, table) => sum + table.added, 0)
}

export function clipsAdded(tables: RestoreTableRow[]): number {
  return tables.find((table) => table.name === 'clips')?.added ?? 0
}

/** Why some clips are set aside and what becomes of the rest, as sentences.
 * Worded to read the same before a restore ("what will happen") and after it
 * ("what was done"), so the summary and the report cannot disagree. */
export function clipNotes(clips: RestoreClipBreakdown, clipFolder: string): string[] {
  const notes: string[] = []
  if (clips.already_here > 0) {
    notes.push(`${plural(clips.already_here, 'clip')} already in this library — left exactly as they are.`)
  }
  if (clips.same_file > 0) {
    notes.push(`${plural(clips.same_file, 'clip')} skipped — another record already tracks the same video.`)
  }
  if (clips.standins_replaced > 0) {
    notes.push(
      `${plural(clips.standins_replaced, 'placeholder record')} the library scan made from video files — ` +
        'replaced by the real records.',
    )
  }
  if (clips.without_video > 0) {
    notes.push(
      `${plural(clips.without_video, 'clip')} with no video on this device — restored as records only, with ` +
        `analysis, stars and tags intact. Copy the videos back to ${clipFolder} and they play again.`,
    )
  }
  return notes
}

const FILE_STATUS_TEXT: Record<RestoreFileStatus, string> = {
  new: 'Will be restored',
  exists: 'Already here — kept',
  merge: 'Only what is missing is added',
  restored: 'Restored',
  replaced: 'Replaced',
  kept: 'Already here — kept',
  merged: 'Merged',
  skipped: 'Skipped',
  failed: 'Failed',
}

export function fileStatusText(status: RestoreFileStatus): string {
  return FILE_STATUS_TEXT[status]
}

/** PrimeVue Tag severity for a file's status. */
export function fileStatusSeverity(status: RestoreFileStatus): 'success' | 'secondary' | 'warn' | 'danger' {
  if (status === 'failed') return 'danger'
  if (status === 'skipped') return 'warn'
  if (status === 'exists' || status === 'kept') return 'secondary'
  return 'success'
}
