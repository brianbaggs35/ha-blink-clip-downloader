import type { SecuritySeverity } from '../../api/types'

/** Shared severity presentation, so the Security tab, the clip modal's AI
 *  panel and anything added later describe the same band identically.
 *  PrimeVue `Tag` severities are reused rather than inventing colours: the
 *  theme already maps them, including in dark mode. */
export const SEVERITY_LABEL: Record<SecuritySeverity, string> = {
  routine: 'Routine',
  noteworthy: 'Noteworthy',
  suspicious: 'Suspicious',
  critical: 'Critical',
}

export const SEVERITY_TAG: Record<SecuritySeverity, string> = {
  routine: 'secondary',
  noteworthy: 'info',
  suspicious: 'warn',
  critical: 'danger',
}

const SEVERITY_RANK: Record<SecuritySeverity, number> = {
  routine: 0,
  noteworthy: 1,
  suspicious: 2,
  critical: 3,
}

/** A severity a future build invented must never outrank one this build
 *  knows, so an unrecognized value ranks lowest — the same rule the backend
 *  applies in security/events.py. */
export function severityRank(severity: string): number {
  return SEVERITY_RANK[severity as SecuritySeverity] ?? 0
}

export function severityLabel(severity: string): string {
  return SEVERITY_LABEL[severity as SecuritySeverity] ?? severity
}

export function severityTag(severity: string): string {
  return SEVERITY_TAG[severity as SecuritySeverity] ?? 'secondary'
}

/** "possible_vehicle_impact" → "Possible vehicle impact". Event types are
 *  snake_case identifiers on the wire; nothing about them should reach a
 *  user unformatted. */
export function formatEventType(eventType: string): string {
  if (!eventType) return ''
  const spaced = eventType.replaceAll('_', ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/** Clip-relative seconds → "0:07". Event offsets are positions within a
 *  clip, not wall-clock times, and showing them as raw seconds invites
 *  reading them as the latter. */
export function formatOffset(seconds: number): string {
  const safe = Math.max(0, Math.round(seconds))
  const minutes = Math.floor(safe / 60)
  return `${minutes}:${String(safe % 60).padStart(2, '0')}`
}

/** An evidence-quality score as a plain word, matching the backend's own
 *  bands so the two never disagree in front of the user. */
export function evidenceLabel(score: number): string {
  if (score < 0.4) return 'weak'
  if (score < 0.7) return 'moderate'
  return 'strong'
}
