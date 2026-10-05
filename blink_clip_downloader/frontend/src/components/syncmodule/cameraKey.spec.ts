import { describe, expect, it } from 'vitest'
import { cameraKey } from './cameraKey'

describe('cameraKey', () => {
  it('is the same for the same module and camera', () => {
    expect(cameraKey('Home', 'Porch')).toBe(cameraKey('Home', 'Porch'))
  })

  it('tells same-named cameras on different sync modules apart', () => {
    expect(cameraKey('Home', 'Porch')).not.toBe(cameraKey('Garage', 'Porch'))
  })

  it('cannot be forged by a name that contains the other half', () => {
    expect(cameraKey('Home', 'Porch')).not.toBe(cameraKey('HomePorch', ''))
    expect(cameraKey('a', 'b c')).not.toBe(cameraKey('a b', 'c'))
  })
})
