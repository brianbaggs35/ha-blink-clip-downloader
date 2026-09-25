/** Leave the SPA for another page on this server — the login page, which
 * media_server/access.py renders itself. Its own module so specs can mock
 * it: jsdom neither navigates nor lets `window.location` be replaced. */
export function goTo(url: string): void {
  window.location.assign(url)
}

/** The login page, returning to where this browser is now once signed in. */
export function loginUrl(): string {
  return `/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`
}
