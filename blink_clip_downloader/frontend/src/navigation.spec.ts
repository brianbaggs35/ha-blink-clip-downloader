import { describe, expect, it } from 'vitest'
import { goTo, loginUrl } from './navigation'

describe('navigation', () => {
  it('goTo() navigates (jsdom only implements hash navigation)', () => {
    goTo('#signed-out')
    expect(window.location.hash).toBe('#signed-out')
  })

  it('loginUrl() returns to the current page and query', () => {
    window.history.replaceState(null, '', '/?kiosk=1&tab=securityfeed')
    expect(loginUrl()).toBe('/login?next=%2F%3Fkiosk%3D1%26tab%3Dsecurityfeed')
    window.history.replaceState(null, '', '/')
  })
})
