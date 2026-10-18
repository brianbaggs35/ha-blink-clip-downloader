import { describe, expect, it } from 'vitest'
import type { RestoreFileStatus } from '../../api/types'
import { breakdown, row } from './testing'
import { clipNotes, clipsAdded, fileStatusSeverity, fileStatusText, nonEmpty, plural, totalAdded } from './words'

describe('backup wording', () => {
  it('counts in words, with thousands separators and the right plural', () => {
    expect(plural(1, 'clip')).toBe('1 clip')
    expect(plural(0, 'clip')).toBe('0 clips')
    expect(plural(1204, 'clip')).toBe('1,204 clips')
  })

  it('leaves out a table the backup has nothing for', () => {
    const tables = [row('clips', 5, 5), row('security_events', 0, 0)]
    expect(nonEmpty(tables).map((t) => t.name)).toEqual(['clips'])
  })

  it('adds up what was added, and picks out the clips', () => {
    const tables = [row('clips', 10, 4), row('analysis_results', 8, 3)]
    expect(totalAdded(tables)).toBe(7)
    expect(clipsAdded(tables)).toBe(4)
  })

  it('counts no clips when the backup has no clips table', () => {
    expect(clipsAdded([row('camera_baselines', 2, 2)])).toBe(0)
    expect(totalAdded([])).toBe(0)
  })

  it('says nothing when nothing needs explaining', () => {
    expect(clipNotes(breakdown(), '/share/clips')).toEqual([])
  })

  it('explains every reason a clip was set aside, and what becomes of the rest', () => {
    const notes = clipNotes(
      breakdown({ already_here: 20, same_file: 1, standins_replaced: 2, without_video: 100 }),
      '/share/blink-clips',
    )
    expect(notes).toEqual([
      '20 clips already in this library — left exactly as they are.',
      '1 clip skipped — another record already tracks the same video.',
      '2 placeholder records the library scan made from video files — replaced by the real records.',
      '100 clips with no video on this device — restored as records only, with analysis, stars and tags intact. ' +
        'Copy the videos back to /share/blink-clips and they play again.',
    ])
  })

  it.each<[RestoreFileStatus, string, string]>([
    ['new', 'Will be restored', 'success'],
    ['exists', 'Already here — kept', 'secondary'],
    ['merge', 'Only what is missing is added', 'success'],
    ['restored', 'Restored', 'success'],
    ['replaced', 'Replaced', 'success'],
    ['kept', 'Already here — kept', 'secondary'],
    ['merged', 'Merged', 'success'],
    ['skipped', 'Skipped', 'warn'],
    ['failed', 'Failed', 'danger'],
  ])('%s reads as "%s" and shows as %s', (status, text, severity) => {
    expect(fileStatusText(status)).toBe(text)
    expect(fileStatusSeverity(status)).toBe(severity)
  })
})
