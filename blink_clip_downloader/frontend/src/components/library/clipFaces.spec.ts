import { describe, expect, it } from 'vitest'
import { clipFaceLine } from './clipFaces'

describe('clipFaceLine', () => {
  it.each([
    ['no_faces', 'No face was found'],
    ['recognized', 'approved household member'],
    ['set_aside', 'put down to the household members'],
    ['unfamiliar', 'A clear face matched nobody enrolled'],
    ['not_approved', 'not approved'],
    ['unexamined', "couldn't be checked"],
    ['unclear', 'no household member seen nearby'],
    ['unclear_ignorable', 'Ignore Unclear Faces'],
    ['extra_people', 'more people were in view'],
  ])('explains %s', (outcome, fragment) => {
    expect(clipFaceLine({ face_outcome: outcome })).toContain(fragment)
  })

  it('adds the counts when faces were seen', () => {
    expect(clipFaceLine({ face_outcome: 'unfamiliar', faces_seen: 3, faces_matched: 1 })).toMatch(
      /\(3 faces seen, 1 matched\.\)$/,
    )
    expect(clipFaceLine({ face_outcome: 'unfamiliar', faces_seen: 1, faces_matched: 0 })).toMatch(
      /\(1 face seen, 0 matched\.\)$/,
    )
  })

  it('has no counts to add when none were seen, or when they were not recorded', () => {
    expect(clipFaceLine({ face_outcome: 'no_faces', faces_seen: 0, faces_matched: 0 })).not.toContain('seen,')
    expect(clipFaceLine({ face_outcome: 'no_faces' })).not.toContain('seen,')
    // Seen but not matched is recorded as zero, not left blank, in practice;
    // a row missing the field entirely still reads as none matched.
    expect(clipFaceLine({ face_outcome: 'unfamiliar', faces_seen: 2 })).toMatch(/\(2 faces seen, 0 matched\.\)$/)
  })

  it('says nothing when recognition examined nothing or the outcome is unknown', () => {
    expect(clipFaceLine(null)).toBeNull()
    expect(clipFaceLine({})).toBeNull()
    expect(clipFaceLine({ face_outcome: '' })).toBeNull()
    expect(clipFaceLine({ face_outcome: 'something_from_a_newer_build' })).toBeNull()
    // Not a lookup into a plain object's prototype.
    expect(clipFaceLine({ face_outcome: 'constructor' })).toBeNull()
  })
})
