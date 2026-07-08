import { afterEach, describe, expect, it, vi } from 'vitest'

async function loadEnv(ingressRoot: string | undefined) {
  vi.resetModules()
  if (ingressRoot === undefined) {
    delete (window as { __HA_INGRESS_ROOT__?: string }).__HA_INGRESS_ROOT__
  } else {
    window.__HA_INGRESS_ROOT__ = ingressRoot
  }
  return import('./env')
}

describe('INGRESS_ROOT', () => {
  afterEach(() => {
    delete (window as { __HA_INGRESS_ROOT__?: string }).__HA_INGRESS_ROOT__
  })

  it('treats the unreplaced __HAROOT__ placeholder (npm run dev) as no prefix', async () => {
    const { INGRESS_ROOT } = await loadEnv('__HAROOT__')
    expect(INGRESS_ROOT).toBe('')
  })

  it('treats a missing global as no prefix', async () => {
    const { INGRESS_ROOT } = await loadEnv(undefined)
    expect(INGRESS_ROOT).toBe('')
  })

  it('passes through a real ingress path substituted by media_server.py', async () => {
    const { INGRESS_ROOT } = await loadEnv('/api/hassio_ingress/abc123')
    expect(INGRESS_ROOT).toBe('/api/hassio_ingress/abc123')
  })
})
