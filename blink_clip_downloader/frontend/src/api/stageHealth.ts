import type { StageHealthReport } from './types'

/** The wait until the next load attempt in words: "on the next clip" when it
 *  is due, seconds up to a minute and a half, whole minutes after that. */
export function retryWording(seconds: number): string {
  if (seconds <= 0) return 'on the next clip'
  if (seconds < 90) return `in ${seconds} second${seconds === 1 ? '' : 's'}`
  return `in ${Math.round(seconds / 60)} minutes`
}

/** The stages whose models failed to load, in the order the server listed them. */
export function failedStages(reports: StageHealthReport[] | null | undefined): StageHealthReport[] {
  return (reports ?? []).filter((report) => report.state === 'failed')
}

/** "face recognition" as a sentence opener: the server names stages in lower case. */
export function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1)
}
