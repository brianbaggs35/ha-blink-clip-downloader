// What face recognition made of one clip, in a sentence — the per-clip
// counterpart of the Biometrics tab's Recognition results panel, which only
// shows totals. It exists so a flagged clip answers "why wasn't I recognized"
// where the question comes up: someone at a front door with their back to the
// camera, a visitor who needs enrolling and a blurred frame of a housemate all
// end the same way (no Recognized badge) for very different reasons.
//
// The outcome values mirror face_outcomes.py's FaceOutcome, stored with every
// analysis since 6.1.1. A row from before that, or from a build with an outcome
// this one doesn't know, says nothing rather than guessing.

import type { AnalysisResultDict } from '../../api/types'

const OUTCOME_TEXT = new Map<string, string>([
  [
    'no_faces',
    'No face was found in the frames checked — nobody was facing the camera, or they were too far away or too small to detect.',
  ],
  ['recognized', 'Everyone whose face was seen is an approved household member.'],
  [
    'set_aside',
    'Recognized: faces too blurred, cut off or small to read were put down to the household members seen beside them.',
  ],
  ['unfamiliar', 'A clear face matched nobody enrolled — a visitor, or someone to enroll on the Biometrics tab.'],
  ['not_approved', 'Someone enrolled but not approved for clearing alerts was in this clip.'],
  ['unexamined', "A frame couldn't be checked for faces; the add-on log says why."],
  ['unclear', 'Faces too blurred, cut off or small to tell, with no household member seen nearby for them to be.'],
  [
    'unclear_ignorable',
    'Faces too blurred, cut off or small to tell. Ignore Unclear Faces (add-on Configuration) would have counted this clip as recognized.',
  ],
  [
    'extra_people',
    "Every face was accounted for, but more people were in view at once than faces recognized — someone's face never showed.",
  ],
])

type ClipFaces = Pick<AnalysisResultDict, 'face_outcome' | 'faces_seen' | 'faces_matched'>

/** One sentence on what recognition made of the clip, or `null` when it
 *  examined nothing (off, nobody enrolled, or an analysis from before 6.1.1)
 *  or there is no analysis yet. */
export function clipFaceLine(result: ClipFaces | null): string | null {
  if (!result) return null
  const text = OUTCOME_TEXT.get(result.face_outcome ?? '')
  if (!text) return null
  const seen = result.faces_seen ?? 0
  if (seen === 0) return text
  return `${text} (${seen} ${seen === 1 ? 'face' : 'faces'} seen, ${result.faces_matched ?? 0} matched.)`
}
