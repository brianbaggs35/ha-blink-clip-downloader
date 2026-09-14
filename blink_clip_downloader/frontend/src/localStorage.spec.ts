import { afterEach, describe, expect, it, vi } from 'vitest'
import { readLocal, removeLocal, writeLocal } from './localStorage'

describe('localStorage helpers', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    localStorage.clear()
  })

  it('round-trips a value', () => {
    writeLocal('k', 'v')
    expect(readLocal('k')).toBe('v')
    removeLocal('k')
    expect(readLocal('k')).toBeNull()
  })

  it('reads as absent when the browser refuses access', () => {
    // Safari with "Block All Cookies", or site data blocked for this
    // origin: the *property access* throws, not the call.
    vi.stubGlobal('localStorage', {
      getItem: () => {
        throw new Error('SecurityError')
      },
      setItem: () => {
        throw new Error('SecurityError')
      },
      removeItem: () => {
        throw new Error('SecurityError')
      },
    })
    expect(readLocal('k')).toBeNull()
    expect(() => writeLocal('k', 'v')).not.toThrow()
    expect(() => removeLocal('k')).not.toThrow()
  })
})
