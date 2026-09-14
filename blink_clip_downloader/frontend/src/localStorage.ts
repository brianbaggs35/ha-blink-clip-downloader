/** Reads and writes to `localStorage` that cannot take the app down with them.
 *
 *  Accessing `localStorage` is not guaranteed to work. Safari with "Block
 *  All Cookies" set, a browser with site data blocked for this origin, and
 *  some embedded webviews all throw `SecurityError` on the *first property
 *  access* — not on the read itself. Two of this app's callers run at
 *  module scope while a Pinia store is being created, so an unguarded
 *  access there does not degrade a preference: it fails the store's
 *  constructor and the whole UI never mounts.
 *
 *  Every helper here degrades to "no stored value" instead, which is
 *  exactly the right behaviour for the handful of per-browser conveniences
 *  this app keeps (theme, collapsed nav, collapsed filters, whether browser
 *  notifications were turned on). None of them are data worth an error
 *  message, so failures are deliberately silent.
 */

export function readLocal(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function writeLocal(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* Preference not persisted — it still applies for this page's lifetime. */
  }
}

export function removeLocal(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* See writeLocal. */
  }
}
