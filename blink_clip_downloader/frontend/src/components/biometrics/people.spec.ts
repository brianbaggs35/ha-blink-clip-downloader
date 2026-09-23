import { describe, expect, it } from 'vitest'
import type { FaceEnrollment } from '../../api/types'
import { groupPeople, initials, isLegacyPhoto, isResolutionMismatch, photoProblems, photoSource } from './people'

function photo(overrides: Partial<FaceEnrollment> = {}): FaceEnrollment {
  return {
    id: 1,
    name: 'Brian',
    created_at: '2026-01-02T00:00:00Z',
    approved: true,
    has_thumbnail: true,
    frame_width: 640,
    camera: 'Front Door',
    warning: null,
    ...overrides,
  }
}

describe('groupPeople', () => {
  it('groups photos by name, alphabetically', () => {
    const people = groupPeople(
      [photo({ id: 1, name: 'Zoe' }), photo({ id: 2, name: 'Amy' }), photo({ id: 3, name: 'Zoe' })],
      640,
    )
    expect(people.map((p) => [p.name, p.photos.map((f) => f.id)])).toEqual([
      ['Amy', [2]],
      ['Zoe', [1, 3]],
    ])
  })

  it('treats a person as approved only when every photo is', () => {
    const [mixed] = groupPeople([photo({ id: 1 }), photo({ id: 2, approved: false })], 640)
    expect(mixed.approved).toBe(false)
    expect(mixed.mixedApproval).toBe(true)
    const [none] = groupPeople([photo({ approved: false })], 640)
    expect(none.mixedApproval).toBe(false)
    const [all] = groupPeople([photo(), photo({ id: 2 })], 640)
    expect(all.approved).toBe(true)
  })

  it('reports the earliest enrollment date', () => {
    const [person] = groupPeople(
      [photo({ id: 1, created_at: '2026-03-01T00:00:00Z' }), photo({ id: 2, created_at: '2026-01-01T00:00:00Z' })],
      640,
    )
    expect(person.firstEnrolled).toBe('2026-01-01T00:00:00Z')
  })

  it('counts photos with problems, but not merely old ones', () => {
    const [person] = groupPeople(
      [
        photo({ id: 1, warning: { unlike_others: true, also_matches: '' } }),
        photo({ id: 2, frame_width: 1280 }),
        photo({ id: 3, has_thumbnail: false, frame_width: null }),
        photo({ id: 4 }),
      ],
      640,
    )
    expect(person.problemCount).toBe(2)
    expect(person.onlyLegacy).toBe(false)
  })

  it('counts photos by where they came from: cameras most first, uploads last', () => {
    const [person] = groupPeople(
      [
        photo({ id: 1, camera: 'Front Door' }),
        photo({ id: 2, frame_width: null, camera: null }),
        photo({ id: 3, camera: 'Driveway' }),
        photo({ id: 4, camera: 'Backyard' }),
        photo({ id: 5, camera: 'Driveway' }),
        photo({ id: 6, has_thumbnail: false, frame_width: null, camera: null }),
      ],
      640,
    )
    expect(person.sources).toEqual([
      { source: { kind: 'camera', camera: 'Driveway' }, count: 2 },
      { source: { kind: 'camera', camera: 'Backyard' }, count: 1 },
      { source: { kind: 'camera', camera: 'Front Door' }, count: 1 },
      { source: { kind: 'upload' }, count: 1 },
    ])
  })

  it('knows when every photo predates stored thumbnails', () => {
    const [person] = groupPeople([photo({ has_thumbnail: false, frame_width: null })], 640)
    expect(person.onlyLegacy).toBe(true)
    expect(person.problemCount).toBe(0)
  })
})

describe('photo checks', () => {
  it('flags a clip photo captured at another width, never an uploaded one', () => {
    expect(isResolutionMismatch(photo({ frame_width: 960 }), 640)).toBe(true)
    expect(isResolutionMismatch(photo({ frame_width: 640 }), 640)).toBe(false)
    expect(isResolutionMismatch(photo({ frame_width: null }), 640)).toBe(false)
  })

  it('says where a photo came from, when that was recorded', () => {
    expect(photoSource(photo({ camera: 'Driveway' }))).toEqual({ kind: 'camera', camera: 'Driveway' })
    expect(photoSource(photo({ camera: null, frame_width: null }))).toEqual({ kind: 'upload' })
    expect(photoSource(photo({ camera: null, has_thumbnail: false, frame_width: null }))).toBeNull()
    // From a clip, before its camera was recorded: not an upload.
    expect(photoSource(photo({ camera: null }))).toBeNull()
  })

  it('recognizes a photo from before thumbnails were kept', () => {
    expect(isLegacyPhoto(photo({ has_thumbnail: false }))).toBe(true)
    expect(isLegacyPhoto(photo())).toBe(false)
  })

  it('explains each problem in plain words', () => {
    const problems = photoProblems(
      photo({ frame_width: 960, warning: { unlike_others: true, also_matches: 'Amy' } }),
      1280,
    )
    expect(problems).toHaveLength(3)
    expect(problems[0]).toContain("Doesn't look like")
    expect(problems[1]).toContain('Also looks like Amy')
    expect(problems[2]).toContain('Captured at 960px')
    expect(problems[2]).toContain('1280px')
    expect(photoProblems(photo(), 640)).toEqual([])
  })
})

describe('initials', () => {
  it('takes the first and last word', () => {
    expect(initials('Mary Ann Smith')).toBe('MS')
    expect(initials('brian')).toBe('B')
    expect(initials('  ')).toBe('?')
    expect(initials('Zoë Ölund')).toBe('ZÖ')
  })
})
