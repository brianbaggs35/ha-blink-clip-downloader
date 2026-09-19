/** Dashboard generation: getting Blink camera tiles onto a Lovelace view
 * (and from there onto a Nest Hub or Chromecast).
 *
 * Two routes, because they trade off differently:
 *
 * - **Camera entities.** Each Blink camera becomes a Generic Camera in Home
 *   Assistant pointing at this add-on's own snapshot endpoint. More setup
 *   (the Generic Camera integration is UI-only — it has had no YAML form
 *   since 2022 — so the entities cannot be generated for you), but the
 *   result is a real `camera.*` entity: Home Assistant fetches the image
 *   server-side, so anything that can show a camera can show it, casting
 *   included.
 * - **An iframe of this add-on's own Security Feed.** No Home Assistant
 *   setup at all — the tab renders itself in kiosk mode (see App.vue's
 *   `?kiosk=1&tab=securityfeed`). The viewer's browser loads it directly,
 *   so it needs to reach the add-on's port, and an http:// add-on inside an
 *   https:// dashboard is blocked as mixed content.
 */

import { slugify } from './types'

export interface DashboardOptions {
  cameras: string[]
  addonUrl: string
  columns: number
  viewTitle: string
  viewPath: string
  includeStorage: boolean
  includeStatus: boolean
  mode: 'cameras' | 'iframe'
}

export const DEFAULT_DASHBOARD_OPTIONS: DashboardOptions = {
  cameras: [],
  addonUrl: 'http://homeassistant.local:8099',
  columns: 2,
  viewTitle: 'Blink Security',
  viewPath: 'security-feed',
  includeStorage: true,
  includeStatus: true,
  mode: 'cameras',
}

function trimUrl(addonUrl: string): string {
  return (addonUrl || DEFAULT_DASHBOARD_OPTIONS.addonUrl).trim().replace(/\/+$/, '')
}

/** The still-image URL for one camera, encoded for a camera name with
 * spaces or punctuation in it. */
export function snapshotUrl(addonUrl: string, camera: string): string {
  return `${trimUrl(addonUrl)}/api/security-feed/snapshot/${encodeURIComponent(camera)}`
}

/** The kiosk URL of this add-on's own Security Feed tab. */
export function kioskUrl(addonUrl: string): string {
  return `${trimUrl(addonUrl)}/?kiosk=1&tab=securityfeed`
}

/** What Home Assistant will name the entity for a Generic Camera added with
 * the suggested "Blink <camera>" name. */
export function cameraEntityId(camera: string): string {
  return `camera.blink_${slugify(camera)}`
}

/** The per-camera name/URL pairs to paste into the Generic Camera dialog. */
export function cameraSetupSheet(options: DashboardOptions): string {
  const cameras = options.cameras.length ? options.cameras : ['<no cameras found>']
  return [
    '# Settings → Devices & services → Add integration → Generic Camera',
    '# Add one per camera, using the name and Still Image URL below.',
    '# Leave "Stream source" empty, and turn OFF "limit refetch to url change"',
    '# so the tile keeps updating.',
    '',
    ...cameras.flatMap((camera) => [
      `Name:             Blink ${camera}`,
      `Still Image URL:  ${snapshotUrl(options.addonUrl, camera)}`,
      `Resulting entity: ${cameraEntityId(camera)}`,
      '',
    ]),
  ]
    .join('\n')
    .trimEnd()
}

function cameraCards(options: DashboardOptions): string[] {
  if (options.mode === 'iframe') {
    return ['      - type: iframe', `        url: ${kioskUrl(options.addonUrl)}`, '        aspect_ratio: 75%']
  }
  const cameras = options.cameras.length ? options.cameras : ['Front Door']
  return [
    '      - type: grid',
    `        columns: ${Math.max(1, Math.round(options.columns))}`,
    '        square: false',
    '        cards:',
    ...cameras.flatMap((camera) => [
      '          - type: picture-entity',
      `            entity: ${cameraEntityId(camera)}`,
      `            name: ${camera}`,
      '            camera_view: auto',
      '            show_state: false',
    ]),
  ]
}

function storageCards(): string[] {
  return [
    '      - type: grid',
    '        columns: 2',
    '        square: false',
    '        cards:',
    '          - type: gauge',
    '            entity: sensor.blink_local_storage',
    '            name: Local storage',
    '            min: 0',
    '            max: 100',
    '            needle: false',
    '            severity:',
    '              green: 0',
    '              yellow: 70',
    '              red: 90',
    '          - type: gauge',
    '            entity: sensor.blink_cloud_storage',
    '            name: Cloud backup',
    '            min: 0',
    '            max: 100',
    '            needle: false',
    '            severity:',
    '              green: 0',
    '              yellow: 70',
    '              red: 90',
  ]
}

function statusCards(): string[] {
  return [
    '      - type: entities',
    '        title: Blink Clip Downloader',
    '        entities:',
    '          - entity: sensor.blink_downloader_status',
    '            name: Clips downloaded',
    '          - type: attribute',
    '            entity: sensor.blink_downloader_status',
    '            attribute: last_download',
    '            name: Last download',
    '          - type: attribute',
    '            entity: sensor.blink_cloud_storage',
    '            attribute: pending_uploads',
    '            name: Waiting to back up',
    '          - entity: script.blink_sync_now',
    '            name: Sync clips now',
  ]
}

/** The Lovelace view, ready to paste into the dashboard's raw configuration
 * editor (three-dot menu → Edit dashboard → Raw configuration editor). */
export function dashboardYaml(options: DashboardOptions): string {
  return [
    'views:',
    `  - title: ${options.viewTitle || DEFAULT_DASHBOARD_OPTIONS.viewTitle}`,
    `    path: ${options.viewPath || DEFAULT_DASHBOARD_OPTIONS.viewPath}`,
    '    icon: mdi:cctv',
    '    cards:',
    ...cameraCards(options),
    ...(options.includeStorage ? storageCards() : []),
    ...(options.includeStatus ? statusCards() : []),
  ].join('\n')
}

/** The cast script for this particular view — the same shape as the Scripts
 * tab's version, pre-filled with the dashboard/view just generated so the
 * paths cannot drift apart. */
export function castScriptYaml(options: DashboardOptions, mediaPlayer: string): string {
  return [
    '# scripts.yaml',
    'blink_cast_cameras:',
    '  alias: Blink – cast the camera view',
    '  icon: mdi:cast',
    '  mode: restart',
    '  sequence:',
    '    - action: cast.show_lovelace_view',
    '      data:',
    `        entity_id: ${mediaPlayer || 'media_player.nest_hub'}`,
    '        dashboard_path: blink',
    `        view_path: ${options.viewPath || DEFAULT_DASHBOARD_OPTIONS.viewPath}`,
  ].join('\n')
}
