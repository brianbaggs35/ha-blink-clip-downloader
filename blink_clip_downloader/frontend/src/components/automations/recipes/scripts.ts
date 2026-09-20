/** Scripts, scenes and helpers — the pieces automations call rather than
 * things that trigger on their own.
 *
 * Same recipe shape as the automations catalogue, which is what lets one
 * builder component render both. The YAML each one produces goes somewhere
 * different, so every recipe says where in its `target`.
 */

import { ADDON_URL_DEFAULT, CLOUD_STORAGE_SENSOR, LOCAL_STORAGE_SENSOR, STATUS_SENSOR } from './shared'
import {
  type Recipe,
  type RecipeValues,
  boolValue,
  duration,
  entityList,
  joinLines,
  listValue,
  numberValue,
  slugify,
  stringValue,
  trimTrailingChar,
  yamlString,
  yamlTemplate,
} from './types'

const syncNow: Recipe = {
  id: 'script-sync-now',
  name: 'Sync clips now',
  group: 'Scripts',
  icon: '⬇️',
  description:
    "Gives Home Assistant a button for the Library tab's Sync — useful in a dashboard, a voice assistant routine, or right after you arrive home.",
  target: 'configuration.yaml',
  filename: 'blink-sync-now.yaml',
  fields: [
    {
      key: 'addon_url',
      label: 'Add-on URL',
      type: 'text',
      default: ADDON_URL_DEFAULT,
      help: 'The direct-access port from the add-on Configuration tab (default 8099). Home Assistant calls this itself, so a LAN address is fine.',
    },
    {
      key: 'confirm',
      label: 'Leave a notification when it runs',
      type: 'toggle',
      default: false,
    },
  ],
  build: (v: RecipeValues) => {
    const url = trimTrailingChar(stringValue(v, 'addon_url', ADDON_URL_DEFAULT), '/')
    const downloadNowUrl = `${url}/api/download-now`
    return joinLines([
      '# configuration.yaml',
      'rest_command:',
      '  blink_sync_now:',
      `    url: ${yamlString(downloadNowUrl)}`,
      '    method: post',
      '    timeout: 30',
      '\n# scripts.yaml (or under script: in configuration.yaml)',
      'blink_sync_now:',
      '  alias: Blink – sync clips now',
      '  icon: mdi:cloud-download',
      '  mode: single',
      '  sequence:',
      '    - action: rest_command.blink_sync_now',
      boolValue(v, 'confirm') ? '    - action: persistent_notification.create' : '',
      boolValue(v, 'confirm') ? '      data:' : '',
      boolValue(v, 'confirm') ? '        title: "Blink"' : '',
      boolValue(v, 'confirm') ? '        message: "Asked the add-on to check Blink for new clips."' : '',
    ])
  },
}

const castFeedScript: Recipe = {
  id: 'script-cast-feed',
  create: { kind: 'script', objectId: 'blink_show_cameras' },
  name: 'Show the camera feed on a display',
  group: 'Scripts',
  icon: '📺',
  description:
    'Casts your Blink dashboard view to a Nest Hub or Chromecast on demand — from a dashboard button, or by asking your voice assistant to run it.',
  target: 'scripts.yaml',
  filename: 'blink-cast-feed-script.yaml',
  fields: [
    {
      key: 'media_player',
      label: 'Display',
      type: 'text',
      default: 'media_player.nest_hub',
    },
    {
      key: 'dashboard_path',
      label: 'Dashboard path',
      type: 'text',
      default: 'blink',
    },
    {
      key: 'view_path',
      label: 'View path',
      type: 'text',
      default: 'security-feed',
    },
    {
      key: 'stop_after',
      label: 'Stop casting after',
      type: 'number',
      default: 5,
      min: 0,
      max: 120,
      suffix: 'min',
      help: '0 leaves it up.',
    },
  ],
  build: (v: RecipeValues) => {
    const player = stringValue(v, 'media_player', 'media_player.nest_hub')
    const stopAfter = numberValue(v, 'stop_after', 5)
    return joinLines([
      '# scripts.yaml',
      'blink_show_cameras:',
      '  alias: Blink – show cameras on the display',
      '  icon: mdi:cast',
      '  mode: restart',
      '  sequence:',
      '    - action: cast.show_lovelace_view',
      '      data:',
      `        entity_id: ${yamlString(player)}`,
      `        dashboard_path: ${yamlString(stringValue(v, 'dashboard_path', 'blink'))}`,
      `        view_path: ${yamlString(stringValue(v, 'view_path', 'security-feed'))}`,
      stopAfter > 0 ? `    - delay: ${yamlString(duration(stopAfter))}` : '',
      stopAfter > 0 ? '    - action: media_player.turn_off' : '',
      stopAfter > 0 ? '      target:' : '',
      stopAfter > 0 ? `        entity_id: ${yamlString(player)}` : '',
    ])
  },
}

const storageReport: Recipe = {
  id: 'script-storage-report',
  create: { kind: 'script', objectId: 'blink_storage_report' },
  name: 'Read out a storage report',
  group: 'Scripts',
  icon: '🗣️',
  description:
    'Speaks (or pushes) how full local and cloud storage are. Pairs well with a voice assistant: "Hey Google, run Blink storage report".',
  target: 'scripts.yaml',
  filename: 'blink-storage-report.yaml',
  fields: [
    {
      key: 'mode',
      label: 'Deliver by',
      type: 'select',
      default: 'tts',
      options: [
        { label: 'Speaking it on a speaker', value: 'tts' },
        { label: 'A notification', value: 'notify' },
      ],
    },
    {
      key: 'tts_entity',
      label: 'Text-to-speech entity',
      type: 'text',
      default: 'tts.google_en_com',
      help: 'Only used when speaking.',
    },
    {
      key: 'media_player',
      label: 'Speaker',
      type: 'text',
      default: 'media_player.kitchen',
    },
    {
      key: 'notify_service',
      label: 'Notify service',
      type: 'text',
      default: 'notify.notify',
      help: 'Only used when notifying.',
    },
  ],
  build: (v: RecipeValues) => {
    const spoken = stringValue(v, 'mode', 'tts') === 'tts'
    const message = `Blink has downloaded {{ states('${STATUS_SENSOR}') }} clips. Local storage is {{ states('${LOCAL_STORAGE_SENSOR}') }} percent full, and cloud backup is {{ states('${CLOUD_STORAGE_SENSOR}') }} percent full with {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'pending_uploads') }} clips waiting to upload.`
    return joinLines([
      '# scripts.yaml',
      'blink_storage_report:',
      '  alias: Blink – storage report',
      '  icon: mdi:database-search',
      '  mode: single',
      '  sequence:',
      spoken
        ? '    - action: tts.speak'
        : `    - action: ${yamlString(stringValue(v, 'notify_service', 'notify.notify'))}`,
      spoken ? '      target:' : '',
      spoken ? `        entity_id: ${yamlString(stringValue(v, 'tts_entity', 'tts.google_en_com'))}` : '',
      '      data:',
      spoken
        ? `        media_player_entity_id: ${yamlString(stringValue(v, 'media_player', 'media_player.kitchen'))}`
        : '',
      spoken ? '' : '        title: "Blink storage"',
      `        message: ${yamlTemplate(message, 8)}`,
    ])
  },
}

const securityScene: Recipe = {
  id: 'scene-security-alert',
  create: { kind: 'scene', objectId: 'blink_security_alert' },
  name: 'Security alert lighting scene',
  group: 'Scenes',
  icon: '🚨',
  description:
    'One scene the automations can call instead of listing lights in each of them — change the look here and every alert follows.',
  target: 'scenes.yaml',
  filename: 'blink-security-alert-scene.yaml',
  fields: [
    {
      key: 'lights',
      label: 'Lights',
      type: 'text',
      default: 'light.porch, light.driveway',
      help: 'Comma separated light entity ids.',
    },
    {
      key: 'brightness',
      label: 'Brightness',
      type: 'number',
      default: 100,
      min: 1,
      max: 100,
      suffix: '%',
    },
    {
      key: 'color',
      label: 'Colour',
      type: 'select',
      default: 'red',
      options: [
        { label: 'Alarm red', value: 'red' },
        { label: 'Amber', value: 'amber' },
        { label: 'Cold white', value: 'white' },
        { label: 'Leave the colour alone', value: 'none' },
      ],
    },
  ],
  build: (v: RecipeValues) => {
    const lights = entityList(v, 'lights', 'light.porch')
    const brightness = Math.round((numberValue(v, 'brightness', 100) / 100) * 255)
    const colors: Record<string, string> = {
      red: '[255, 40, 40]',
      amber: '[255, 170, 60]',
      white: '[255, 255, 255]',
    }
    const rgb = colors[stringValue(v, 'color', 'red')]
    return joinLines([
      '# scenes.yaml',
      '- id: blink_security_alert',
      '  name: Blink security alert',
      '  icon: mdi:alarm-light',
      '  entities:',
      ...lights.flatMap((entity) =>
        [
          `    ${yamlString(entity)}:`,
          '      state: "on"',
          `      brightness: ${brightness}`,
          rgb ? `      rgb_color: ${rgb}` : '',
        ].filter(Boolean),
      ),
      '\n# Call it from any automation with:',
      '#   - action: scene.turn_on',
      '#     target:',
      '#       entity_id: scene.blink_security_alert',
    ])
  },
}

const pauseHelper: Recipe = {
  id: 'helper-pause-alerts',
  name: 'A switch that pauses Blink alerts',
  group: 'Helpers',
  icon: '🔕',
  description:
    'A toggle for the times you know you are the one on camera — mowing the lawn, unloading the car — without editing automations.',
  target: 'configuration.yaml',
  filename: 'blink-pause-helper.yaml',
  fields: [
    {
      key: 'name',
      label: 'Helper name',
      type: 'text',
      default: 'Blink alerts paused',
    },
    {
      key: 'auto_resume',
      label: 'Turn itself back on after',
      type: 'number',
      default: 60,
      min: 0,
      max: 1440,
      suffix: 'min',
      help: '0 means it stays off until you flip it back — the setting that has quietly cost people a break-in alert.',
    },
  ],
  build: (v: RecipeValues) => {
    const autoResume = numberValue(v, 'auto_resume', 60)
    return joinLines([
      '# configuration.yaml',
      'input_boolean:',
      '  blink_alerts_paused:',
      `    name: ${yamlString(stringValue(v, 'name', 'Blink alerts paused'))}`,
      '    icon: mdi:bell-off',
      '\n# Add this condition to any generated automation to respect it:',
      '#   - condition: state',
      '#     entity_id: input_boolean.blink_alerts_paused',
      '#     state: "off"',
      autoResume > 0 ? '\n# automations.yaml — un-pauses itself so a forgotten toggle' : '',
      autoResume > 0 ? '# cannot silently disable your alerts forever.' : '',
      autoResume > 0 ? 'alias: "Blink – resume alerts automatically"' : '',
      autoResume > 0 ? 'mode: restart' : '',
      autoResume > 0 ? 'triggers:' : '',
      autoResume > 0 ? '  - trigger: state' : '',
      autoResume > 0 ? '    entity_id: input_boolean.blink_alerts_paused' : '',
      autoResume > 0 ? '    to: "on"' : '',
      autoResume > 0 ? `    for: ${yamlString(duration(autoResume))}` : '',
      autoResume > 0 ? 'conditions: []' : '',
      autoResume > 0 ? 'actions:' : '',
      autoResume > 0 ? '  - action: input_boolean.turn_off' : '',
      autoResume > 0 ? '    target:' : '',
      autoResume > 0 ? '      entity_id: input_boolean.blink_alerts_paused' : '',
    ])
  },
}

const templateSensors: Recipe = {
  id: 'helper-template-sensors',
  name: 'Storage health template sensors',
  group: 'Helpers',
  icon: '📊',
  description:
    'Turns the two storage percentages into a plain ok / warning / critical state and a problem binary sensor, which is what dashboards and alert cards actually want to read.',
  target: 'configuration.yaml',
  filename: 'blink-template-sensors.yaml',
  fields: [
    {
      key: 'warning',
      label: 'Warning above',
      type: 'number',
      default: 80,
      min: 1,
      max: 99,
      suffix: '%',
    },
    {
      key: 'critical',
      label: 'Critical above',
      type: 'number',
      default: 95,
      min: 1,
      max: 99,
      suffix: '%',
    },
  ],
  build: (v: RecipeValues) => {
    const warning = numberValue(v, 'warning', 80)
    const critical = numberValue(v, 'critical', 95)
    const worst = `[states('${LOCAL_STORAGE_SENSOR}') | float(0), states('${CLOUD_STORAGE_SENSOR}') | float(0)] | max`
    const healthState = `{% set worst = ${worst} %}{{ 'critical' if worst >= ${critical} else ('warning' if worst >= ${warning} else 'ok') }}`
    const worstPercent = `{{ ${worst} }}`
    const problemState = `{{ (${worst}) >= ${warning} }}`
    return joinLines([
      '# configuration.yaml',
      'template:',
      '  - sensor:',
      '      - name: Blink storage health',
      '        unique_id: blink_storage_health',
      '        icon: mdi:database-check',
      `        state: ${yamlTemplate(healthState, 8)}`,
      '        attributes:',
      `          worst_percent: ${yamlTemplate(worstPercent, 10)}`,
      '  - binary_sensor:',
      '      - name: Blink storage problem',
      '        unique_id: blink_storage_problem',
      '        device_class: problem',
      `        state: ${yamlTemplate(problemState, 8)}`,
    ])
  },
}

const armSyncModule: Recipe = {
  id: 'script-arm-sync',
  name: 'Arm or disarm the Sync Module',
  group: 'Scripts',
  icon: '🛡️',
  description:
    "Gives Home Assistant control of Blink's own arming, so presence, a dashboard button or a voice command can do it — and so the arm-on-away automation has something to call.",
  target: 'configuration.yaml',
  filename: 'blink-arm-sync.yaml',
  fields: [
    {
      key: 'addon_url',
      label: 'Add-on URL',
      type: 'text',
      default: ADDON_URL_DEFAULT,
      help: 'The direct-access port (default 8099). Home Assistant calls this itself, so a LAN address is fine.',
    },
    {
      key: 'sync_module',
      label: 'Sync Module name',
      type: 'text',
      default: 'My Sync Module',
      help: 'Exactly as it appears on the Sync Module tab.',
    },
  ],
  build: (v: RecipeValues) => {
    const url = trimTrailingChar(stringValue(v, 'addon_url', ADDON_URL_DEFAULT), '/')
    const name = stringValue(v, 'sync_module', 'My Sync Module')
    const armUrl = `${url}/api/sync-modules/${encodeURIComponent(name)}/arm`
    return joinLines([
      '# configuration.yaml',
      'rest_command:',
      '  blink_sync_arm:',
      `    url: ${yamlString(armUrl)}`,
      '    method: post',
      '    content_type: "application/json"',
      `    payload: ${yamlString('{"armed": true}')}`,
      '    timeout: 30',
      '  blink_sync_disarm:',
      `    url: ${yamlString(armUrl)}`,
      '    method: post',
      '    content_type: "application/json"',
      `    payload: ${yamlString('{"armed": false}')}`,
      '    timeout: 30',
      '\n# scripts.yaml — a button-friendly wrapper around each one.',
      'blink_arm_sync_module:',
      '  alias: Blink – arm the Sync Module',
      '  icon: mdi:shield-check',
      '  mode: single',
      '  sequence:',
      '    - action: rest_command.blink_sync_arm',
      'blink_disarm_sync_module:',
      '  alias: Blink – disarm the Sync Module',
      '  icon: mdi:shield-off',
      '  mode: single',
      '  sequence:',
      '    - action: rest_command.blink_sync_disarm',
    ])
  },
}

const archiveNow: Recipe = {
  id: 'script-archive-now',
  name: 'Archive old clips now',
  group: 'Scripts',
  icon: '🗜️',
  description:
    "Runs the add-on's archiver on demand instead of waiting for its next sweep — and gives the storage-threshold automation something to call.",
  target: 'configuration.yaml',
  filename: 'blink-archive-now.yaml',
  fields: [
    {
      key: 'addon_url',
      label: 'Add-on URL',
      type: 'text',
      default: ADDON_URL_DEFAULT,
    },
  ],
  build: (v: RecipeValues) => {
    const url = trimTrailingChar(stringValue(v, 'addon_url', ADDON_URL_DEFAULT), '/')
    const archiveUrl = url + '/api/storage/archive/run-now'
    return joinLines([
      '# configuration.yaml',
      'rest_command:',
      '  blink_archive_now:',
      `    url: ${yamlString(archiveUrl)}`,
      '    method: post',
      '    timeout: 120',
      '\n# scripts.yaml',
      'blink_archive_now:',
      '  alias: Blink – archive old clips now',
      '  icon: mdi:archive-arrow-down',
      '  mode: single',
      '  sequence:',
      '    - action: rest_command.blink_archive_now',
    ])
  },
}

const snapshotAll: Recipe = {
  id: 'script-snapshot-all',
  create: { kind: 'script', objectId: 'blink_camera_snapshots' },
  name: 'Send a snapshot of every camera',
  group: 'Scripts',
  icon: '📸',
  description:
    'One tap and every camera lands on your phone — for "did I leave the garage open?", or as the first thing an alarm automation does.',
  target: 'scripts.yaml',
  filename: 'blink-camera-snapshots.yaml',
  fields: [
    {
      key: 'cameras',
      label: 'Cameras',
      type: 'multiselect',
      default: [] as string[],
      source: 'cameras' as const,
      help: 'Pick the ones to include — this builds one message per camera, so it cannot mean "all" on its own. Needs the Generic Camera entities from the Dashboards tab.',
    },
    {
      key: 'notify_service',
      label: 'Notify service',
      type: 'text',
      default: 'notify.notify',
      help: 'A mobile_app service is what renders the images inline.',
    },
  ],
  build: (v: RecipeValues) => {
    const cameras = listValue(v, 'cameras')
    const names = cameras.length ? cameras : ['Front Door']
    const service = stringValue(v, 'notify_service', 'notify.notify')
    return joinLines([
      '# scripts.yaml',
      'blink_camera_snapshots:',
      '  alias: Blink – send every camera snapshot',
      '  icon: mdi:camera-burst',
      '  mode: single',
      '  sequence:',
      ...names.flatMap((camera) => [
        `    - action: ${yamlString(service)}`,
        '      data:',
        `        title: ${yamlString('📸 ' + camera)}`,
        `        message: ${yamlString('Latest snapshot from ' + camera + '.')}`,
        '        data:',
        `          image: /api/camera_proxy/camera.blink_${slugify(camera)}`,
      ]),
    ])
  },
}

const allClearScene: Recipe = {
  id: 'scene-all-clear',
  create: { kind: 'scene', objectId: 'blink_all_clear' },
  name: 'All-clear lighting scene',
  group: 'Scenes',
  icon: '🌙',
  description:
    'The other half of the alert scene: puts the lights back afterwards, so an automation can end an alert as cleanly as it started one.',
  target: 'scenes.yaml',
  filename: 'blink-all-clear-scene.yaml',
  fields: [
    {
      key: 'lights',
      label: 'Lights',
      type: 'text',
      default: 'light.porch, light.driveway',
      help: 'The same ones the alert scene changes.',
    },
    {
      key: 'leave_on',
      label: 'Leave them on, dimmed',
      type: 'toggle',
      default: false,
      help: 'Off turns them out entirely; on returns them to a low warm level.',
    },
    {
      key: 'brightness',
      label: 'Dimmed to',
      type: 'number',
      default: 30,
      min: 1,
      max: 100,
      suffix: '%',
    },
  ],
  build: (v: RecipeValues) => {
    const lights = entityList(v, 'lights', 'light.porch')
    const leaveOn = boolValue(v, 'leave_on')
    const brightness = Math.round((numberValue(v, 'brightness', 30) / 100) * 255)
    return joinLines([
      '# scenes.yaml',
      '- id: blink_all_clear',
      '  name: Blink all clear',
      '  icon: mdi:lightbulb-off-outline',
      '  entities:',
      ...lights.flatMap((entity) =>
        leaveOn
          ? [`    ${yamlString(entity)}:`, '      state: "on"', `      brightness: ${brightness}`]
          : [`    ${yamlString(entity)}:`, '      state: "off"'],
      ),
      '\n# Call it from the end of an alert automation with:',
      '#   - action: scene.turn_on',
      '#     target:',
      '#       entity_id: scene.blink_all_clear',
    ])
  },
}

export const SCRIPT_RECIPES: Recipe[] = [
  syncNow,
  armSyncModule,
  archiveNow,
  castFeedScript,
  storageReport,
  snapshotAll,
  securityScene,
  allClearScene,
  pauseHelper,
  templateSensors,
]
