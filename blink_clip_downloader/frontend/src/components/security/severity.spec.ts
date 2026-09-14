import { describe, expect, it } from 'vitest'
import { evidenceLabel, formatEventType, formatOffset, severityLabel, severityRank, severityTag } from './severity'

describe('severity helpers', () => {
  it('ranks severities in ascending order', () => {
    expect(severityRank('routine')).toBe(0)
    expect(severityRank('noteworthy')).toBe(1)
    expect(severityRank('suspicious')).toBe(2)
    expect(severityRank('critical')).toBe(3)
  })

  it('ranks an unknown severity lowest', () => {
    // A severity a newer build invented must never outrank a known one.
    expect(severityRank('apocalyptic')).toBe(0)
  })

  it('labels known severities and passes through unknown ones', () => {
    expect(severityLabel('critical')).toBe('Critical')
    expect(severityLabel('weird')).toBe('weird')
  })

  it('maps severities onto PrimeVue tag severities', () => {
    expect(severityTag('critical')).toBe('danger')
    expect(severityTag('routine')).toBe('secondary')
    expect(severityTag('weird')).toBe('secondary')
  })

  it('formats snake_case event types for display', () => {
    expect(formatEventType('possible_vehicle_impact')).toBe('Possible vehicle impact')
    expect(formatEventType('')).toBe('')
  })

  it('formats clip offsets as minutes and seconds', () => {
    expect(formatOffset(0)).toBe('0:00')
    expect(formatOffset(7.4)).toBe('0:07')
    expect(formatOffset(75)).toBe('1:15')
  })

  it('clamps a negative offset rather than rendering "-0:01"', () => {
    expect(formatOffset(-5)).toBe('0:00')
  })

  it('bands evidence quality the same way the backend does', () => {
    expect(evidenceLabel(0.2)).toBe('weak')
    expect(evidenceLabel(0.4)).toBe('moderate')
    expect(evidenceLabel(0.69)).toBe('moderate')
    expect(evidenceLabel(0.7)).toBe('strong')
  })
})
