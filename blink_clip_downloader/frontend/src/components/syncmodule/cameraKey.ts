/**
 * Identifies one camera on the Sync Module tab. A name alone is not enough:
 * two sync modules can each hold a camera of the same name, and the toggle
 * for one must not show the other as updating.
 */
export function cameraKey(moduleName: string, cameraName: string): string {
  return `${moduleName}\u0000${cameraName}`
}
