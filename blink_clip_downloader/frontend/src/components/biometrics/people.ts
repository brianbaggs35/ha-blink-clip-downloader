import type { FaceEnrollment } from '../../api/types'

/** Everyone enrolled under one name — the unit the Biometrics tab manages. */
export interface Person {
  name: string
  photos: FaceEnrollment[]
  /** True only when every photo is approved (the conservative reading). */
  approved: boolean
  /** Some photos approved and some not — left behind by an older version. */
  mixedApproval: boolean
  firstEnrolled: string
  /** Photos that may be hurting recognition (see photoProblems). */
  problemCount: number
  /** Every photo predates 6.0.7 — worth adding fresh ones from a clip. */
  onlyLegacy: boolean
  /** Where the photos came from, most first — see photoSource. */
  sources: SourceCount[]
}

/** A camera whose clips a photo was found in, or an uploaded photo. */
export type PhotoSource = { kind: 'camera'; camera: string } | { kind: 'upload' }

export interface SourceCount {
  source: PhotoSource
  count: number
}

/**
 * Where one photo came from, or null when that is unknown — a photo
 * enrolled before 6.0.7, which recorded neither.
 */
export function photoSource(photo: FaceEnrollment): PhotoSource | null {
  if (photo.camera) return { kind: 'camera', camera: photo.camera }
  if (photo.has_thumbnail && photo.frame_width == null) return { kind: 'upload' }
  return null
}

export function sourceLabel(source: PhotoSource): string {
  return source.kind === 'camera' ? source.camera : 'Uploaded'
}

/** Cameras by how many photos came from them, then uploads. */
function countSources(photos: FaceEnrollment[]): SourceCount[] {
  const counts = new Map<string, SourceCount>()
  for (const photo of photos) {
    const source = photoSource(photo)
    if (!source) continue
    const key = source.kind === 'camera' ? `camera:${source.camera}` : 'upload'
    const entry = counts.get(key)
    if (entry) entry.count += 1
    else counts.set(key, { source, count: 1 })
  }
  return [...counts.values()].sort(
    (a, b) =>
      Number(a.source.kind === 'upload') - Number(b.source.kind === 'upload') ||
      b.count - a.count ||
      sourceLabel(a.source).localeCompare(sourceLabel(b.source)),
  )
}

/**
 * A photo captured from a clip at a different width than recognition now
 * matches at. Face size decides how well two faces compare, so these match
 * poorly until the person is re-scanned at the current resolution.
 */
export function isResolutionMismatch(photo: FaceEnrollment, frameWidth: number): boolean {
  return photo.frame_width != null && photo.frame_width !== frameWidth
}

/** Enrolled before 6.0.7: from the old picker's smaller frames, no image kept. */
export function isLegacyPhoto(photo: FaceEnrollment): boolean {
  return !photo.has_thumbnail
}

/**
 * Plain-language reasons this photo may be hurting recognition: a wrong
 * face filed under the name, a face that also passes for someone else, or
 * a size recognition no longer works at. Having been enrolled by an older
 * version is deliberately not one — that is true of every photo anyone
 * enrolled before 6.0.7, and flagging all of them would bury the real
 * problems (the card suggests fresh photos instead).
 */
export function photoProblems(photo: FaceEnrollment, frameWidth: number): string[] {
  const problems: string[] = []
  if (photo.warning?.unlike_others) {
    problems.push("Doesn't look like this person's other photos — check it's really them.")
  }
  if (photo.warning?.also_matches) {
    problems.push(`Also looks like ${photo.warning.also_matches} — recognition could confuse the two.`)
  }
  if (isResolutionMismatch(photo, frameWidth)) {
    problems.push(
      `Captured at ${photo.frame_width}px, but recognition now runs at ${frameWidth}px — scan a clip again to add a matching photo.`,
    )
  }
  return problems
}

/** Enrolled photos grouped into people, alphabetically. */
export function groupPeople(faces: FaceEnrollment[], frameWidth: number): Person[] {
  const byName = new Map<string, FaceEnrollment[]>()
  for (const face of faces) {
    const photos = byName.get(face.name)
    if (photos) photos.push(face)
    else byName.set(face.name, [face])
  }
  return [...byName.entries()]
    .map(([name, photos]) => {
      const approvedCount = photos.filter((p) => p.approved).length
      return {
        name,
        photos,
        approved: approvedCount === photos.length,
        mixedApproval: approvedCount > 0 && approvedCount < photos.length,
        firstEnrolled: photos.map((p) => p.created_at).sort((a, b) => a.localeCompare(b))[0],
        problemCount: photos.filter((p) => photoProblems(p, frameWidth).length > 0).length,
        onlyLegacy: photos.every(isLegacyPhoto),
        sources: countSources(photos),
      }
    })
    .sort((a, b) => a.name.localeCompare(b.name))
}

/** Up to two initials for a person without any stored photo. */
export function initials(name: string): string {
  // Array.from, not part[0]: a name can start with a character outside the
  // Basic Multilingual Plane, which part[0] would cut in half.
  const letters = name
    .split(/\s+/)
    .filter(Boolean)
    .map((part) => Array.from(part)[0])
  if (!letters.length) return '?'
  return (letters.length > 1 ? [letters[0], letters.at(-1)].join('') : letters[0]).toUpperCase()
}
