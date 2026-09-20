/** Building blocks shared by more than one recipe.
 *
 * These render *fragments* — a condition, one action — rather than whole
 * documents, so a recipe stays a readable list of the pieces it wants. Every
 * one returns '' when the option it renders is switched off, which is how
 * joinLines() drops it without leaving a gap in the YAML.
 */

import { duration, jinjaList, yamlString, yamlTemplate } from './types'

/** The add-on's own entities, in one place — these strings are a contract
 * with ha_entities.py, not free text (see that module's docstring). */
export const STATUS_SENSOR = 'sensor.blink_downloader_status'
export const LOCAL_STORAGE_SENSOR = 'sensor.blink_local_storage'
export const CLOUD_STORAGE_SENSOR = 'sensor.blink_cloud_storage'
export const CLIP_DOWNLOADED_EVENT = 'blink_clip_downloaded'
export const CLIP_ANALYZED_EVENT = 'blink_clip_analyzed'
export const BATTERY_LOW_EVENT = 'blink_camera_battery_low'

/** Where the add-on answers on the LAN, as a starting point for the fields
 * that ask for it.
 *
 * Plain HTTP on purpose: the direct-access port serves HTTP, and Home
 * Assistant calls it server-side — the same situation as notifier.py's
 * Supervisor API URL, which carries this same marker.
 */
export const ADDON_URL_DEFAULT = 'http://homeassistant.local:8099' // NOSONAR

interface NotifyOptions {
  /** Break through silent mode on the Companion app. */
  critical?: boolean
  /** Attach the camera's own snapshot, via the Generic Camera entities the
   * Dashboards tab sets up. */
  snapshot?: boolean
  /** Where tapping the notification goes. */
  clickPath?: string
}

/** A `notify.*` action, with the Companion app's extras where they earn
 * their place.
 *
 * Every optional block deliberately carries both platforms' keys at once —
 * iOS reads `push.sound.critical` and `url`, Android reads
 * `ttl`/`priority`/`channel` and `clickAction`, and each ignores the
 * other's — so one generated snippet works on whichever phone reads it.
 * They all live under the *inner* `data:`, which is the part the Companion
 * app integration interprets rather than the notify service itself.
 */
export function notifyAction(
  service: string,
  title: string,
  messageTemplate: string,
  options: NotifyOptions = {},
): string {
  const lines = [
    `  - action: ${yamlString(service)}`,
    '    data:',
    `      title: ${yamlString(title)}`,
    `      message: ${yamlTemplate(messageTemplate, 6)}`,
  ]
  const extras: string[] = []
  if (options.critical) {
    extras.push(
      '        push:',
      '          sound:',
      '            name: default',
      '            critical: 1',
      '            volume: 1.0',
      '        ttl: 0',
      '        priority: high',
      '        channel: alarm',
    )
  }
  if (options.snapshot) {
    // Home Assistant's own camera proxy, not the add-on's port: the phone
    // is usually not on the same network, and the proxy URL works wherever
    // the Companion app already reaches Home Assistant.
    extras.push(
      `        image: ${yamlTemplate('/api/camera_proxy/camera.blink_{{ trigger.event.data.camera | slugify }}', 8)}`,
    )
  }
  if (options.clickPath) {
    extras.push(
      `        url: ${yamlString(options.clickPath)}`,
      `        clickAction: ${yamlString(options.clickPath)}`,
    )
  }
  if (extras.length) lines.push('      data:', ...extras)
  return lines.join('\n')
}

/** Restrict an event-triggered automation to specific cameras. Empty means
 * every camera — the same "empty is all" convention the add-on's own camera
 * options use. */
export function cameraCondition(cameras: string[]): string {
  if (!cameras.length) return ''
  const template = `{{ trigger.event.data.camera in ${jinjaList(cameras)} }}`
  return ['  - condition: template', `    value_template: ${yamlTemplate(template, 4)}`].join('\n')
}

/** Restrict to clips that came from a given source (motion, live view, the
 * Sync Module's USB drive). */
export function sourceCondition(sources: string[]): string {
  if (!sources.length) return ''
  const template = `{{ trigger.event.data.source in ${jinjaList(sources)} }}`
  return ['  - condition: template', `    value_template: ${yamlTemplate(template, 4)}`].join('\n')
}

/** A numeric floor on one of the event payload's numbers. */
export function eventNumberCondition(field: string, minimum: number): string {
  if (!minimum) return ''
  const template = `{{ (trigger.event.data.${field} | float(0)) >= ${minimum} }}`
  return ['  - condition: template', `    value_template: ${yamlTemplate(template, 4)}`].join('\n')
}

/** Only act between two times of day. */
export function timeWindowCondition(after: string, before: string): string {
  if (!after && !before) return ''
  return [
    '  - condition: time',
    after && `    after: ${yamlString(after)}`,
    before && `    before: ${yamlString(before)}`,
  ]
    .filter(Boolean)
    .join('\n')
}

/** Skip everything while a "pause Blink alerts" helper is on (see the
 * Scripts & Helpers tab, which generates the input_boolean itself). */
export function pauseSwitchCondition(entity: string): string {
  if (!entity) return ''
  return ['  - condition: state', `    entity_id: ${yamlString(entity)}`, '    state: "off"'].join('\n')
}

/** `conditions:` with its entries, or the empty list when nothing applies —
 * HA accepts a missing `conditions:` key too, but an explicit empty list
 * makes the generated YAML paste cleanly into the UI editor. */
export function conditionsBlock(parts: string[]): string {
  const used = parts.filter(Boolean)
  if (!used.length) return 'conditions: []'
  return ['conditions:', ...used].join('\n')
}

/** `for: "HH:MM:SS"` under a trigger, or nothing when it is zero. */
export function forDuration(minutes: number, indent = 4): string {
  if (minutes <= 0) return ''
  return `${' '.repeat(indent)}for: ${yamlString(duration(minutes))}`
}
