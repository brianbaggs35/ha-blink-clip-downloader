declare global {
  interface Window {
    __HA_INGRESS_ROOT__?: string
  }
}

// media_server.py's index handler string-replaces the `__HAROOT__` literal
// (see index.html) with the current Home Assistant ingress path prefix on
// every request. In `npm run dev` nothing performs that substitution, so the
// literal placeholder survives unreplaced — treat that case the same as "no
// prefix" rather than sending it as a literal path segment.
const raw = window.__HA_INGRESS_ROOT__ ?? ''
export const INGRESS_ROOT = raw === '__HAROOT__' ? '' : raw
