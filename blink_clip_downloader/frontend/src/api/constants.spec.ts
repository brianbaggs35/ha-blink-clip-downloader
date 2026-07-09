import { describe, expect, it } from 'vitest'
import {
  ESCALATION_NOTE,
  PROVIDER_NOTES,
  fmtCost,
  fmtDur,
  fmtNum,
  fmtRelative,
  fmtSize,
  fmtTs,
  providerLabel,
} from './constants'

describe('providerLabel', () => {
  it('maps known providers to their display label', () => {
    expect(providerLabel('anthropic')).toBe('Anthropic (Claude)')
    expect(providerLabel('moondream_local')).toBe('Moondream Local (0.5B)')
  })

  it('falls back to the raw provider string when unknown', () => {
    expect(providerLabel('mystery')).toBe('mystery')
  })

  it('falls back to an em dash when empty/undefined', () => {
    expect(providerLabel(undefined)).toBe('—')
    expect(providerLabel('')).toBe('—')
  })
})

describe('PROVIDER_NOTES / ESCALATION_NOTE', () => {
  it('has copy for every known provider', () => {
    expect(PROVIDER_NOTES.ollama).toContain('Ollama (Local/LAN)')
    expect(PROVIDER_NOTES.moondream_cloud).toContain('moondream.ai')
  })

  it('escalation note mentions ai_escalation_provider', () => {
    expect(ESCALATION_NOTE).toContain('ai_escalation_provider')
  })
})

describe('fmtNum', () => {
  it('formats zero/null/undefined as "0"', () => {
    expect(fmtNum(0)).toBe('0')
    expect(fmtNum(null)).toBe('0')
    expect(fmtNum(undefined)).toBe('0')
  })

  it('formats thousands with a K suffix', () => {
    expect(fmtNum(1500)).toBe('1.5K')
  })

  it('formats millions with an M suffix', () => {
    expect(fmtNum(2_500_000)).toBe('2.50M')
  })

  it('formats small numbers as-is', () => {
    expect(fmtNum(42)).toBe('42')
  })
})

describe('fmtCost', () => {
  it('formats null/undefined as N/A', () => {
    expect(fmtCost(null)).toBe('N/A')
    expect(fmtCost(undefined)).toBe('N/A')
  })

  it('formats sub-cent costs as <$0.001', () => {
    expect(fmtCost(0.0002)).toBe('<$0.001')
  })

  it('formats larger costs to 4 decimal places', () => {
    expect(fmtCost(1.23456)).toBe('$1.2346')
  })
})

describe('fmtTs', () => {
  it('returns an empty string for falsy input', () => {
    expect(fmtTs(null)).toBe('')
    expect(fmtTs(undefined)).toBe('')
    expect(fmtTs('')).toBe('')
  })

  it('formats a valid ISO timestamp via toLocaleString', () => {
    const iso = '2026-01-05T12:00:00Z'
    expect(fmtTs(iso)).toBe(new Date(iso).toLocaleString())
  })

  it('falls back to the raw string if Date parsing throws', () => {
    expect(fmtTs('not-a-real-date')).toBe(new Date('not-a-real-date').toLocaleString())
  })
})

describe('fmtRelative', () => {
  it('returns an empty string for falsy input', () => {
    expect(fmtRelative(null)).toBe('')
    expect(fmtRelative(undefined)).toBe('')
  })

  it('formats recent times as "just now"', () => {
    expect(fmtRelative(new Date().toISOString())).toBe('just now')
  })

  it('formats minutes/hours/days ago', () => {
    const minutesAgo = new Date(Date.now() - 5 * 60_000).toISOString()
    expect(fmtRelative(minutesAgo)).toBe('5m ago')
    const hoursAgo = new Date(Date.now() - 3 * 3_600_000).toISOString()
    expect(fmtRelative(hoursAgo)).toBe('3h ago')
    const daysAgo = new Date(Date.now() - 2 * 86_400_000).toISOString()
    expect(fmtRelative(daysAgo)).toBe('2d ago')
  })
})

describe('fmtSize', () => {
  it('returns an empty string for falsy input', () => {
    expect(fmtSize(0)).toBe('')
    expect(fmtSize(null)).toBe('')
    expect(fmtSize(undefined)).toBe('')
  })

  it('formats bytes in KB/MB/GB', () => {
    expect(fmtSize(2048)).toBe('2 KB')
    expect(fmtSize(5 * 1_048_576)).toBe('5.0 MB')
    expect(fmtSize(2 * 1_073_741_824)).toBe('2.00 GB')
  })
})

describe('fmtDur', () => {
  it('returns an empty string for falsy input', () => {
    expect(fmtDur(0)).toBe('')
    expect(fmtDur(null)).toBe('')
    expect(fmtDur(undefined)).toBe('')
  })

  it('formats seconds only when under a minute', () => {
    expect(fmtDur(45)).toBe('45s')
  })

  it('formats minutes + seconds', () => {
    expect(fmtDur(125)).toBe('2m 5s')
  })
})
