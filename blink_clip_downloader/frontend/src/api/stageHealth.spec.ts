import { describe, expect, it } from 'vitest'
import { capitalize, failedStages, retryWording } from './stageHealth'
import type { StageHealthReport } from './types'

describe('retryWording', () => {
  it.each([
    [0, 'on the next clip'],
    [-5, 'on the next clip'],
    [1, 'in 1 second'],
    [45, 'in 45 seconds'],
    [89, 'in 89 seconds'],
    [90, 'in 2 minutes'],
    [180, 'in 3 minutes'],
    [3600, 'in 60 minutes'],
  ])('words %i seconds as "%s"', (seconds, words) => {
    expect(retryWording(seconds)).toBe(words)
  })
})

describe('failedStages', () => {
  const report = (stage: string, state: StageHealthReport['state']): StageHealthReport => ({
    stage,
    state,
    detail: '',
    permanent: false,
    retry_in: null,
  })

  it('keeps only the stages that failed, in order', () => {
    expect(
      failedStages([report('a', 'failed'), report('b', 'ready'), report('c', 'failed')]).map((r) => r.stage),
    ).toEqual(['a', 'c'])
  })

  it.each([[undefined], [null], [[]]])('is empty for %j', (reports) => {
    expect(failedStages(reports as StageHealthReport[] | null | undefined)).toEqual([])
  })
})

describe('capitalize', () => {
  it.each([
    ['face recognition', 'Face recognition'],
    ['Already', 'Already'],
    ['', ''],
  ])('turns "%s" into "%s"', (input, output) => {
    expect(capitalize(input)).toBe(output)
  })
})
