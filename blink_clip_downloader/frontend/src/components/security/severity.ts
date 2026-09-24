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

/** Per-band accent, as a bare `R G B` triple so one table serves both the
 *  solid colour and every translucent tint of it (`rgb(<triple> / 0.16)`)
 *  without a second set of values to keep in step. Deliberately the same
 *  four hues the theme gives the PrimeVue `Tag` severities above, so a
 *  band's dot, its bar segment and its tag all read as one thing.
 *
 *  Used for backgrounds, rails and dots only — never for text on its own,
 *  where amber on white would not carry in the light theme. */
export const SEVERITY_RGB: Record<SecuritySeverity, string> = {
  routine: '100 116 139',
  noteworthy: '59 130 246',
  suspicious: '245 158 11',
  critical: '239 68 68',
}

/** The band's accent, optionally as a tint of itself. */
export function severityColor(severity: string, alpha = 1): string {
  const rgb = SEVERITY_RGB[severity as SecuritySeverity] ?? SEVERITY_RGB.routine
  return alpha >= 1 ? `rgb(${rgb})` : `rgb(${rgb} / ${alpha})`
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

/** Which protected thing an event was about, for a chip beside it: the
 *  protected vehicle, or an asset marked on the Assets tab by the name it was
 *  given there. Null for events about no asset (someone merely present, a
 *  sound, the camera itself). The vehicle's own name is its description,
 *  which can run to a sentence, so it gets a short fixed label instead. */
export function assetOf(event: { asset_name?: string; asset_type?: string }): { label: string; icon: string } | null {
  if (!event.asset_name) return null
  if (event.asset_type === 'vehicle') return { label: 'Protected vehicle', icon: 'pi pi-car' }
  return { label: event.asset_name, icon: 'pi pi-shield' }
}
