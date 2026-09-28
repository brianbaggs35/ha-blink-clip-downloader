import { describe, expect, it } from 'vitest'
import { goTo, loginUrl } from './navigation'

describe('navigation', () => {
  it('goTo() navigates (jsdom only implements hash navigation)', () => {
    goTo('#signed-out')
    expect(window.location.hash).toBe('#signed-out')
  })

  it('loginUrl() returns to the current page and query', () => {
    window.history.replaceState(null, '', '/?tab=ai')
    expect(loginUrl()).toBe('/login?next=%2F%3Ftab%3Dai')
    window.history.replaceState(null, '', '/')
  })

  it('loginUrl() keeps a kiosk card framable', () => {
    window.history.replaceState(null, '', '/?kiosk=1&tab=securityfeed')
    expect(loginUrl()).toBe('/login?next=%2F%3Fkiosk%3D1%26tab%3Dsecurityfeed&kiosk=1')
    window.history.replaceState(null, '', '/')
  })
})
