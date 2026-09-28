/** Leave the SPA for another page on this server — the login page, which
 * media_server/access.py renders itself. Its own module so specs can mock
 * it: jsdom neither navigates nor lets `window.location` be replaced. */
export function goTo(url: string): void {
  window.location.assign(url)
}

/** The login page, returning to where this browser is now once signed in.
 *
 * Keeps `kiosk=1` on the login page's own URL, as the server's redirect to it
 * does (media_server/access.py): a dashboard card's frame may only show a
 * page that says kiosk=1 itself, so without it a sign-in that expired inside
 * the card would leave the card blank. */
export function loginUrl(): string {
  const here = window.location.pathname + window.location.search
  const url = `/login?next=${encodeURIComponent(here)}`
  const kiosk = new URLSearchParams(window.location.search).get('kiosk') === '1'
  return kiosk ? `${url}&kiosk=1` : url
}
