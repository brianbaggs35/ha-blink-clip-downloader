import { describe, expect, it } from 'vitest'
import type { ClipListItem } from '../../api/types'
import { collapseDuplicateClips, fmtOffset, groupMatch, qualityLevel, type FoundFace } from './found'

function face(id: string, match: string | null = null): FoundFace {
  return {
    id,
    thumbnail: '',
    quality: 0.8,
    width: 60,
    time: 0,
    match: match ? { name: match, similarity: 0.8 } : null,
    source: { kind: 'photo', label: 'p.jpg' },
  }
}

function clip(id: string, camera: string, timestamp: string, source = 'pir'): ClipListItem {
  return {
    id,
    camera,
    timestamp,
    source,
    file_path: '',
    size_bytes: 0,
    duration: 10,
    network_id: 0,
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
  }
}

describe('qualityLevel', () => {
  it('buckets the score', () => {
    expect(qualityLevel(0.9)).toBe('good')
    expect(qualityLevel(0.6)).toBe('good')
    expect(qualityLevel(0.4)).toBe('fair')
    expect(qualityLevel(0.1)).toBe('low')
  })
})

describe('fmtOffset', () => {
  it('formats seconds into the clip', () => {
    expect(fmtOffset(0)).toBe('0:00')
    expect(fmtOffset(7.5)).toBe('0:07')
    expect(fmtOffset(75)).toBe('1:15')
  })
})

describe('groupMatch', () => {
  it('names a group only when most of its faces match the same person', () => {
    expect(groupMatch([face('a', 'Brian'), face('b', 'Brian'), face('c')])).toBe('Brian')
    expect(groupMatch([face('a', 'Brian'), face('b'), face('c')])).toBeNull()
    expect(groupMatch([face('a', 'Brian'), face('b', 'Amy')])).toBeNull()
    expect(groupMatch([face('a')])).toBeNull()
  })
})

describe('collapseDuplicateClips', () => {
  it("keeps the cloud copy of an event also saved to the Sync Module's USB drive", () => {
    const { clips, hidden } = collapseDuplicateClips([
      clip('local_1', 'Front Door', '2026-01-01T10:00:01+00:00', 'local_storage'),
      clip('cloud_1', 'Front Door', '2026-01-01T10:00:00+00:00'),
      clip('other', 'Backyard', '2026-01-01T10:00:00+00:00', 'local_storage'),
    ])
    expect(clips.map((c) => c.id).sort()).toEqual(['cloud_1', 'other'])
    expect(hidden).toBe(1)
  })

  it('never merges clips further apart than any real retrigger', () => {
    const { clips, hidden } = collapseDuplicateClips([
      clip('a', 'Front Door', '2026-01-01T10:00:00Z'),
      clip('b', 'Front Door', '2026-01-01T10:00:10Z'),
    ])
    expect(clips).toHaveLength(2)
    expect(hidden).toBe(0)
  })

  it('lists newest first, whatever the timestamp format', () => {
    const { clips } = collapseDuplicateClips([
      clip('older', 'A', '2026-01-01T09:00:00+00:00'),
      clip('newer', 'B', '2026-01-01T10:00:00Z'),
    ])
    expect(clips.map((c) => c.id)).toEqual(['newer', 'older'])
  })
})
