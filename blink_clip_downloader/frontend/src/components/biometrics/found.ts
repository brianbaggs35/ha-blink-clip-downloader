import type { ClipListItem, FaceCandidate } from '../../api/types'

/** Where a found face came from, for its caption and for grouping labels. */
export type FaceSource =
  { kind: 'clip'; clipId: string; camera: string; timestamp: string } | { kind: 'photo'; label: string }

export interface FoundFace extends FaceCandidate {
  source: FaceSource
}

export type QualityLevel = 'good' | 'fair' | 'low'

/**
 * Buckets for vision/faces.py's quality score. Below "fair" a face is too
 * small or blurred to be a useful reference — measured, a slightly blurred
 * 40px face still matched itself, a more blurred one did not — so those
 * are hidden until asked for.
 */
export function qualityLevel(quality: number): QualityLevel {
  if (quality >= 0.6) return 'good'
  if (quality >= 0.35) return 'fair'
  return 'low'
}

export const QUALITY_LABEL: Record<QualityLevel, string> = { good: 'Good', fair: 'Fair', low: 'Low quality' }
export const QUALITY_SEVERITY: Record<QualityLevel, 'success' | 'info' | 'warn'> = {
  good: 'success',
  fair: 'info',
  low: 'warn',
}

/** "0:07" — how far into its clip a face was found. */
export function fmtOffset(seconds: number): string {
  const whole = Math.floor(seconds)
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/**
 * The name most of a group's faces are already recognized as, if any —
 * so a group can be labelled "Already recognized as Brian".
 */
export function groupMatch(faces: FoundFace[]): string | null {
  const counts = new Map<string, number>()
  for (const face of faces) if (face.match) counts.set(face.match.name, (counts.get(face.match.name) ?? 0) + 1)
  let best: string | null = null
  let bestCount = 0
  for (const [name, count] of counts) {
    if (count > bestCount) [best, bestCount] = [name, count]
  }
  return bestCount * 2 > faces.length ? best : null
}

// A clip and its copy from the Sync Module's USB drive start at the same
// moment on the same camera. No camera can start two genuine clips this
// close together — Blink's shortest retrigger time is 10 seconds.
const SAME_EVENT_SECONDS = 5

/**
 * *clips* with each event listed once, newest first. With a Blink
 * subscription and a USB drive, the Sync Module backs cloud clips up to the
 * drive and both copies get downloaded — the same event twice, which the
 * picker used to show as two identical-looking clips. The cloud copy is
 * kept, since it is the one the rest of the library treats as primary.
 */
export function collapseDuplicateClips(clips: ClipListItem[]): { clips: ClipListItem[]; hidden: number } {
  const ordered = [...clips].sort((a, b) => Number(a.source === 'local_storage') - Number(b.source === 'local_storage'))
  const kept: ClipListItem[] = []
  for (const clip of ordered) {
    const at = new Date(clip.timestamp).getTime()
    const duplicate = kept.some(
      (k) => k.camera === clip.camera && Math.abs(new Date(k.timestamp).getTime() - at) <= SAME_EVENT_SECONDS * 1000,
    )
    if (!duplicate) kept.push(clip)
  }
  kept.sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
  return { clips: kept, hidden: clips.length - kept.length }
}
