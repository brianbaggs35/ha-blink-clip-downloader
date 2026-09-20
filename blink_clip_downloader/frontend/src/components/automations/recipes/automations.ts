/** The automation catalogue.
 *
 * Every entity id and event name used here is one the add-on really
 * publishes (see blink_downloader/ha_entities.py and app.py) — nothing in
 * this file is illustrative. The YAML is emitted in Home Assistant's current
 * `triggers:`/`conditions:`/`actions:` syntax, which is what the UI editor
 * writes back when you open one of these.
 */

import {
  BATTERY_LOW_EVENT,
  CLIP_ANALYZED_EVENT,
  CLIP_DOWNLOADED_EVENT,
  CLOUD_STORAGE_SENSOR,
  LOCAL_STORAGE_SENSOR,
  STATUS_SENSOR,
  cameraCondition,
  conditionsBlock,
  eventNumberCondition,
  forDuration,
  notifyAction,
  pauseSwitchCondition,
  sourceCondition,
  timeWindowCondition,
} from './shared'
import {
  type Recipe,
  type RecipeValues,
  boolValue,
  duration,
  entityList,
  joinLines,
  listValue,
  numberValue,
  stringValue,
  timeOfDay,
  yamlString,
  yamlTemplate,
} from './types'

const NOTIFY_HELP =
  'Any notify service — notify.notify, notify.mobile_app_<your phone>, notify.persistent_notification.'

const notifyField = (key = 'notify_service') => ({
  key,
  label: 'Notify service',
  type: 'text' as const,
  default: 'notify.notify',
  placeholder: 'notify.mobile_app_pixel',
  help: NOTIFY_HELP,
})

const criticalField = {
  key: 'critical',
  label: 'Send as a high-priority alert',
  type: 'toggle' as const,
  default: false,
  help: 'Adds the Companion app keys that break through silent mode on iOS and Android.',
}

const snapshotField = {
  key: 'snapshot',
  label: "Attach the camera's snapshot",
  type: 'toggle' as const,
  default: false,
  help: 'Uses the Generic Camera entities from the Dashboards tab, served through Home Assistant so it reaches your phone anywhere.',
}

const clickPathField = {
  key: 'click_path',
  label: 'Tapping it opens (optional)',
  type: 'text' as const,
  default: '',
  placeholder: '/hassio/ingress/abc123_blink_clip_downloader',
  help: "Any Home Assistant path. For this add-on's own panel, open it and copy the path out of the browser's address bar.",
}

const pauseField = {
  key: 'pause_entity',
  label: 'Pause switch (optional)',
  type: 'text' as const,
  default: '',
  placeholder: 'input_boolean.blink_alerts_paused',
  help: 'When this helper is on, the automation does nothing. Generate the helper itself on the Scripts & Helpers tab.',
}

const camerasField = {
  key: 'cameras',
  label: 'Cameras',
  type: 'multiselect' as const,
  default: [] as string[],
  source: 'cameras' as const,
  help: 'Leave empty for every camera.',
}

function header(alias: string, description: string, mode = 'single', max?: number): string {
  return joinLines([
    `alias: ${yamlString(alias)}`,
    `description: ${yamlTemplate(description, 0)}`,
    `mode: ${mode}`,
    max !== undefined ? `max: ${max}` : '',
  ])
}

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------

const cloudStorageThreshold: Recipe = {
  id: 'cloud-storage-threshold',
  name: 'Cloud backup storage is filling up',
  group: 'Storage',
  icon: '☁️',
  description:
    'Warns when the cloud account your clips are backed up to passes a percentage you choose, so you find out before uploads start failing.',
  target: 'automations.yaml',
  filename: 'blink-cloud-storage-threshold.yaml',
  fields: [
    {
      key: 'threshold',
      label: 'Notify above',
      type: 'number',
      default: 85,
      min: 1,
      max: 99,
      suffix: '%',
      help: 'Percentage of the connected cloud account that is in use.',
    },
    {
      key: 'sustained',
      label: 'Only after it stays there for',
      type: 'number',
      default: 15,
      min: 0,
      max: 720,
      suffix: 'min',
      help: '0 notifies the moment it crosses. A few minutes avoids a one-off blip while uploads are in flight.',
    },
    {
      key: 'remind_daily',
      label: 'Remind me daily while it stays full',
      type: 'toggle',
      default: true,
    },
    {
      key: 'remind_at',
      label: 'Daily reminder time',
      type: 'time',
      default: '09:00',
    },
    notifyField(),
    criticalField,
  ],
  build: (v: RecipeValues) => {
    const threshold = numberValue(v, 'threshold', 85)
    const remind = boolValue(v, 'remind_daily')
    return joinLines([
      header(
        `Blink – cloud backup storage above ${threshold}%`,
        'Notifies when the cloud backup destination this add-on uploads to passes the chosen fill level.',
      ),
      'triggers:',
      '  - trigger: numeric_state',
      `    entity_id: ${CLOUD_STORAGE_SENSOR}`,
      `    above: ${threshold}`,
      forDuration(numberValue(v, 'sustained', 15)),
      remind ? '  - trigger: time' : '',
      remind ? `    at: ${yamlString(timeOfDay(stringValue(v, 'remind_at', '09:00'), '09:00:00'))}` : '',
      conditionsBlock([
        // The time trigger fires regardless of the level, so the condition
        // is what keeps the daily reminder honest.
        remind
          ? joinLines([
              '  - condition: numeric_state',
              `    entity_id: ${CLOUD_STORAGE_SENSOR}`,
              `    above: ${threshold}`,
            ])
          : '',
      ]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        `☁️ Blink cloud backup is ${threshold}% full`,
        `{{ states('${CLOUD_STORAGE_SENSOR}') }}% of {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'total_gb') }} GB used on {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'provider') }}, with {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'pending_uploads') }} clip(s) still queued to upload.`,
        { critical: boolValue(v, 'critical') },
      ),
    ])
  },
}

const localStorageThreshold: Recipe = {
  id: 'local-storage-threshold',
  name: 'Local clip storage is filling up',
  group: 'Storage',
  icon: '💾',
  description: 'Warns when the clip library passes a percentage of its quota — or of the disk, when no quota is set.',
  target: 'automations.yaml',
  filename: 'blink-local-storage-threshold.yaml',
  fields: [
    {
      key: 'threshold',
      label: 'Notify above',
      type: 'number',
      default: 80,
      min: 1,
      max: 99,
      suffix: '%',
    },
    {
      key: 'sustained',
      label: 'Only after it stays there for',
      type: 'number',
      default: 10,
      min: 0,
      max: 720,
      suffix: 'min',
    },
    notifyField(),
    criticalField,
  ],
  build: (v: RecipeValues) => {
    const threshold = numberValue(v, 'threshold', 80)
    return joinLines([
      header(
        `Blink – local clip storage above ${threshold}%`,
        'Notifies when the local clip library passes the chosen fill level. The sensor measures against the storage quota when one is set, and against the whole disk otherwise.',
      ),
      'triggers:',
      '  - trigger: numeric_state',
      `    entity_id: ${LOCAL_STORAGE_SENSOR}`,
      `    above: ${threshold}`,
      forDuration(numberValue(v, 'sustained', 10)),
      conditionsBlock([]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        `💾 Blink local storage is ${threshold}% full`,
        `{{ states('${LOCAL_STORAGE_SENSOR}') }}% used ({{ state_attr('${LOCAL_STORAGE_SENSOR}', 'basis') }} basis) — {{ state_attr('${LOCAL_STORAGE_SENSOR}', 'clips_used_gb') }} GB of clips, {{ state_attr('${LOCAL_STORAGE_SENSOR}', 'disk_free_gb') }} GB free on disk.`,
        { critical: boolValue(v, 'critical') },
      ),
    ])
  },
}

const uploadBacklog: Recipe = {
  id: 'cloud-upload-backlog',
  name: 'Cloud backups are falling behind',
  group: 'Storage',
  icon: '📤',
  description:
    'Catches a backup queue that stops draining — a paused account, an expired token, or uploads slower than new clips arrive.',
  target: 'automations.yaml',
  filename: 'blink-cloud-upload-backlog.yaml',
  fields: [
    {
      key: 'pending',
      label: 'Alert when more than',
      type: 'number',
      default: 25,
      min: 1,
      max: 5000,
      suffix: 'clips queued',
    },
    {
      key: 'sustained',
      label: 'And it stays that way for',
      type: 'number',
      default: 60,
      min: 0,
      max: 1440,
      suffix: 'min',
      help: 'A backlog right after a big download burst is normal; one that persists is not.',
    },
    notifyField(),
  ],
  build: (v: RecipeValues) => {
    const pending = numberValue(v, 'pending', 25)
    return joinLines([
      header(
        'Blink – cloud backup queue is not draining',
        'Notifies when clips stay queued for cloud backup longer than expected.',
      ),
      'triggers:',
      '  - trigger: numeric_state',
      `    entity_id: ${CLOUD_STORAGE_SENSOR}`,
      '    attribute: pending_uploads',
      `    above: ${pending}`,
      forDuration(numberValue(v, 'sustained', 60)),
      conditionsBlock([]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '📤 Blink cloud backups are falling behind',
        `{{ state_attr('${CLOUD_STORAGE_SENSOR}', 'pending_uploads') }} clip(s) waiting, {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'failed_uploads') }} failed. Uploads paused: {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'uploads_paused') }} {{ state_attr('${CLOUD_STORAGE_SENSOR}', 'pause_reason') }}`,
      ),
    ])
  },
}

// ---------------------------------------------------------------------------
// Security
// ---------------------------------------------------------------------------

const suspiciousAlert: Recipe = {
  id: 'suspicious-clip-alert',
  name: 'Suspicious clip alert',
  group: 'Security',
  icon: '🚨',
  description:
    'Pushes the AI summary the moment a clip is judged suspicious, filtered to the confidence and risk you actually want waking you up.',
  target: 'automations.yaml',
  filename: 'blink-suspicious-clip-alert.yaml',
  fields: [
    camerasField,
    {
      key: 'min_confidence',
      label: 'Minimum confidence',
      type: 'number',
      default: 0.6,
      min: 0,
      max: 1,
      step: 0.05,
      help: 'How sure the model has to be. 0 accepts every suspicious verdict.',
    },
    {
      key: 'min_risk',
      label: 'Minimum risk score',
      type: 'number',
      default: 0,
      min: 0,
      max: 100,
      help: 'From the deterministic security layer, 0-100. Leave at 0 unless object detection is enabled.',
    },
    notifyField(),
    criticalField,
    snapshotField,
    clickPathField,
    pauseField,
  ],
  build: (v: RecipeValues) =>
    joinLines([
      header(
        'Blink – suspicious clip alert',
        'Notifies when the add-on finishes analyzing a clip and judged it suspicious.',
        'queued',
        10,
      ),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${CLIP_ANALYZED_EVENT}`,
      conditionsBlock([
        joinLines([
          '  - condition: template',
          `    value_template: ${yamlTemplate('{{ trigger.event.data.is_suspicious }}', 4)}`,
        ]),
        eventNumberCondition('confidence', numberValue(v, 'min_confidence', 0.6)),
        eventNumberCondition('risk_score', numberValue(v, 'min_risk', 0)),
        cameraCondition(listValue(v, 'cameras')),
        pauseSwitchCondition(stringValue(v, 'pause_entity')),
      ]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '🚨 Blink: suspicious activity',
        '{{ trigger.event.data.camera }} — {{ trigger.event.data.summary }} (confidence {{ (trigger.event.data.confidence | float(0) * 100) | round(0) }}%{% if trigger.event.data.risk_score %}, risk {{ trigger.event.data.risk_score }}{% endif %})',
        {
          critical: boolValue(v, 'critical'),
          snapshot: boolValue(v, 'snapshot'),
          clickPath: stringValue(v, 'click_path'),
        },
      ),
    ]),
}

const securityLights: Recipe = {
  id: 'security-lights',
  name: 'Turn on lights when something looks off',
  group: 'Security',
  icon: '💡',
  description:
    'Switches lights (or any switch) on when a clip is judged suspicious, optionally only after dark, and turns them back off afterwards.',
  target: 'automations.yaml',
  filename: 'blink-security-lights.yaml',
  fields: [
    {
      key: 'lights',
      label: 'Lights or switches',
      type: 'text',
      default: 'light.porch',
      placeholder: 'light.porch, switch.driveway_floods',
      help: 'Comma separated entity ids.',
    },
    camerasField,
    {
      key: 'min_confidence',
      label: 'Minimum confidence',
      type: 'number',
      default: 0.6,
      min: 0,
      max: 1,
      step: 0.05,
    },
    {
      key: 'after_dark',
      label: 'Only after dark',
      type: 'toggle',
      default: true,
      help: "Uses the sun's position, so it follows the seasons without you editing times.",
    },
    {
      key: 'brightness',
      label: 'Brightness',
      type: 'number',
      default: 100,
      min: 1,
      max: 100,
      suffix: '%',
      help: 'Applies to light entities; a switch is simply turned on.',
    },
    {
      key: 'auto_off',
      label: 'Turn back off after',
      type: 'number',
      default: 5,
      min: 0,
      max: 120,
      suffix: 'min',
      help: '0 leaves them on.',
    },
  ],
  build: (v: RecipeValues) => {
    const entities = entityList(v, 'lights', 'light.porch')
    const autoOff = numberValue(v, 'auto_off', 5)
    // Brightness is a light-only option, and homeassistant.turn_on forwards
    // whatever data it is given straight to the domain's own service — so a
    // switch in the same list would be called as switch.turn_on with a
    // brightness it rejects outright. Lights get light.turn_on with the
    // brightness; anything else gets the generic service without it.
    const lights = entities.filter((id) => id.startsWith('light.'))
    const others = entities.filter((id) => !id.startsWith('light.'))
    const target = (ids: string[]) => ['    target:', '      entity_id:', ...ids.map((id) => `        - ${id}`)]
    const offBlock = ['  - action: homeassistant.turn_off', ...target(entities)]
    return joinLines([
      header(
        'Blink – lights on for suspicious activity',
        'Turns lights on when a clip is judged suspicious, then turns them off again.',
        // restart, not single: a second clip during the delay should extend
        // the light, not be dropped as "already running".
        'restart',
      ),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${CLIP_ANALYZED_EVENT}`,
      conditionsBlock([
        joinLines([
          '  - condition: template',
          `    value_template: ${yamlTemplate('{{ trigger.event.data.is_suspicious }}', 4)}`,
        ]),
        eventNumberCondition('confidence', numberValue(v, 'min_confidence', 0.6)),
        cameraCondition(listValue(v, 'cameras')),
        boolValue(v, 'after_dark') ? joinLines(['  - condition: sun', '    after: sunset', '    before: sunrise']) : '',
      ]),
      'actions:',
      ...(lights.length
        ? [
            '  - action: light.turn_on',
            ...target(lights),
            '    data:',
            `      brightness_pct: ${numberValue(v, 'brightness', 100)}`,
          ]
        : []),
      ...(others.length ? ['  - action: homeassistant.turn_on', ...target(others)] : []),
      autoOff > 0 ? `  - delay: ${yamlString(duration(autoOff))}` : '',
      ...(autoOff > 0 ? offBlock : []),
    ])
  },
}

const castFeed: Recipe = {
  id: 'cast-security-feed',
  name: 'Cast the camera feed to a display',
  group: 'Security',
  icon: '📺',
  description:
    'Throws your Blink dashboard view onto a Nest Hub or Chromecast when something suspicious happens, then puts it back to sleep.',
  target: 'automations.yaml',
  filename: 'blink-cast-security-feed.yaml',
  fields: [
    {
      key: 'media_player',
      label: 'Display',
      type: 'text',
      default: 'media_player.nest_hub',
      placeholder: 'media_player.kitchen_display',
      help: 'The Chromecast-based display to cast to.',
    },
    {
      key: 'dashboard_path',
      label: 'Dashboard path',
      type: 'text',
      default: 'blink',
      help: 'The URL segment after /lovelace — see the Dashboards tab, which generates the view itself.',
    },
    {
      key: 'view_path',
      label: 'View path',
      type: 'text',
      default: 'security-feed',
    },
    camerasField,
    {
      key: 'min_confidence',
      label: 'Minimum confidence',
      type: 'number',
      default: 0.6,
      min: 0,
      max: 1,
      step: 0.05,
    },
    {
      key: 'stop_after',
      label: 'Stop casting after',
      type: 'number',
      default: 3,
      min: 0,
      max: 120,
      suffix: 'min',
      help: '0 leaves the feed up until something else takes the screen.',
    },
  ],
  build: (v: RecipeValues) => {
    const player = stringValue(v, 'media_player', 'media_player.nest_hub')
    const stopAfter = numberValue(v, 'stop_after', 3)
    return joinLines([
      header(
        'Blink – cast the camera feed on suspicious activity',
        'Casts the Blink dashboard view to a display when a clip is judged suspicious.',
        'restart',
      ),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${CLIP_ANALYZED_EVENT}`,
      conditionsBlock([
        joinLines([
          '  - condition: template',
          `    value_template: ${yamlTemplate('{{ trigger.event.data.is_suspicious }}', 4)}`,
        ]),
        eventNumberCondition('confidence', numberValue(v, 'min_confidence', 0.6)),
        cameraCondition(listValue(v, 'cameras')),
      ]),
      'actions:',
      '  - action: cast.show_lovelace_view',
      '    data:',
      `      entity_id: ${player}`,
      `      dashboard_path: ${yamlString(stringValue(v, 'dashboard_path', 'blink'))}`,
      `      view_path: ${yamlString(stringValue(v, 'view_path', 'security-feed'))}`,
      stopAfter > 0 ? `  - delay: ${yamlString(duration(stopAfter))}` : '',
      stopAfter > 0 ? '  - action: media_player.turn_off' : '',
      stopAfter > 0 ? '    target:' : '',
      stopAfter > 0 ? `      entity_id: ${player}` : '',
    ])
  },
}

const announceClip: Recipe = {
  id: 'announce-clip',
  name: 'Announce a clip on a speaker',
  group: 'Security',
  icon: '🔊',
  description: 'Speaks which camera just recorded, on the speakers you choose, within the hours you choose.',
  target: 'automations.yaml',
  filename: 'blink-announce-clip.yaml',
  fields: [
    {
      key: 'tts_entity',
      label: 'Text-to-speech entity',
      type: 'text',
      default: 'tts.google_en_com',
      help: 'From Settings → Devices & services → Entities, filtered to the tts domain.',
    },
    {
      key: 'media_player',
      label: 'Speaker',
      type: 'text',
      default: 'media_player.kitchen',
    },
    camerasField,
    {
      key: 'suspicious_only',
      label: 'Only announce suspicious clips',
      type: 'toggle',
      default: true,
      help: 'Off announces every downloaded clip, which is a lot on a busy camera.',
    },
    {
      key: 'after',
      label: 'Not before',
      type: 'time',
      default: '07:00',
    },
    {
      key: 'before',
      label: 'Not after',
      type: 'time',
      default: '22:00',
    },
  ],
  build: (v: RecipeValues) => {
    const suspiciousOnly = boolValue(v, 'suspicious_only')
    return joinLines([
      header('Blink – announce a clip on a speaker', 'Speaks an announcement when a Blink clip arrives.', 'queued', 5),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${suspiciousOnly ? CLIP_ANALYZED_EVENT : CLIP_DOWNLOADED_EVENT}`,
      conditionsBlock([
        suspiciousOnly
          ? joinLines([
              '  - condition: template',
              `    value_template: ${yamlTemplate('{{ trigger.event.data.is_suspicious }}', 4)}`,
            ])
          : '',
        cameraCondition(listValue(v, 'cameras')),
        timeWindowCondition(
          timeOfDay(stringValue(v, 'after', '07:00'), '07:00:00'),
          timeOfDay(stringValue(v, 'before', '22:00'), '22:00:00'),
        ),
      ]),
      'actions:',
      '  - action: tts.speak',
      '    target:',
      `      entity_id: ${stringValue(v, 'tts_entity', 'tts.google_en_com')}`,
      '    data:',
      `      media_player_entity_id: ${stringValue(v, 'media_player', 'media_player.kitchen')}`,
      `      message: ${yamlTemplate(
        suspiciousOnly
          ? '{{ trigger.event.data.summary }}'
          : 'The {{ trigger.event.data.camera }} camera just recorded a clip.',
        6,
      )}`,
    ])
  },
}

// ---------------------------------------------------------------------------
// Clips
// ---------------------------------------------------------------------------

const newClipNotify: Recipe = {
  id: 'new-clip-notify',
  name: 'Notify on a new clip',
  group: 'Clips',
  icon: '🎥',
  description:
    'The classic: a push notification whenever a clip is downloaded, narrowed to the cameras, sources and hours you care about.',
  target: 'automations.yaml',
  filename: 'blink-new-clip-notify.yaml',
  fields: [
    camerasField,
    {
      key: 'sources',
      label: 'Sources',
      type: 'multiselect',
      default: [] as string[],
      options: [
        { label: 'Motion (pir)', value: 'pir' },
        { label: 'Button press', value: 'button_press' },
        { label: 'Live View recording', value: 'liveview' },
        { label: 'Sync Module storage', value: 'local_storage' },
      ],
      help: 'Leave empty for every source.',
    },
    {
      key: 'after',
      label: 'Not before',
      type: 'time',
      default: '',
      placeholder: '07:00',
      help: 'Leave both times empty for around the clock.',
    },
    {
      key: 'before',
      label: 'Not after',
      type: 'time',
      default: '',
      placeholder: '22:00',
    },
    notifyField(),
    snapshotField,
    clickPathField,
    pauseField,
  ],
  build: (v: RecipeValues) => {
    const after = stringValue(v, 'after')
    const before = stringValue(v, 'before')
    return joinLines([
      header('Blink – new clip notification', 'Notifies whenever the add-on downloads a clip.', 'queued', 10),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${CLIP_DOWNLOADED_EVENT}`,
      conditionsBlock([
        cameraCondition(listValue(v, 'cameras')),
        sourceCondition(listValue(v, 'sources')),
        // '' fallback, not the usual one: an empty or half-typed time here
        // means "no limit", and quietly inventing 08:00 would silently drop
        // every notification outside it.
        timeWindowCondition(timeOfDay(after, ''), timeOfDay(before, '')),
        pauseSwitchCondition(stringValue(v, 'pause_entity')),
      ]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '🎥 New Blink clip',
        '{{ trigger.event.data.camera }} — {{ trigger.event.data.duration | int(0) }}s, {{ (trigger.event.data.size_bytes | int(0) / 1048576) | round(1) }} MB ({{ trigger.event.data.source }})',
        {
          snapshot: boolValue(v, 'snapshot'),
          clickPath: stringValue(v, 'click_path'),
        },
      ),
    ])
  },
}

const longClip: Recipe = {
  id: 'long-motion-clip',
  name: 'Alert on an unusually long clip',
  group: 'Clips',
  icon: '⏱️',
  description: 'A long recording usually means motion that did not stop — worth a look even before the AI gets to it.',
  target: 'automations.yaml',
  filename: 'blink-long-motion-clip.yaml',
  fields: [
    {
      key: 'seconds',
      label: 'Longer than',
      type: 'number',
      default: 20,
      min: 1,
      max: 300,
      suffix: 'sec',
    },
    camerasField,
    notifyField(),
  ],
  build: (v: RecipeValues) => {
    const seconds = numberValue(v, 'seconds', 20)
    const longerThan = `{{ (trigger.event.data.duration | float(0)) > ${seconds} }}`
    return joinLines([
      header(`Blink – clip longer than ${seconds}s`, 'Notifies when a downloaded clip runs longer than expected.'),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${CLIP_DOWNLOADED_EVENT}`,
      conditionsBlock([
        joinLines(['  - condition: template', `    value_template: ${yamlTemplate(longerThan, 4)}`]),
        cameraCondition(listValue(v, 'cameras')),
      ]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '⏱️ Long Blink clip',
        '{{ trigger.event.data.camera }} recorded for {{ trigger.event.data.duration | int(0) }} seconds.',
      ),
    ])
  },
}

// ---------------------------------------------------------------------------
// Maintenance
// ---------------------------------------------------------------------------

const batteryLow: Recipe = {
  id: 'camera-battery-low',
  name: 'Camera battery went low',
  group: 'Maintenance',
  icon: '🔋',
  description: 'Fires the first time a camera drops to low battery — once per transition, not once per poll.',
  target: 'automations.yaml',
  filename: 'blink-camera-battery-low.yaml',
  fields: [
    camerasField,
    notifyField(),
    {
      key: 'persistent',
      label: 'Also leave a notification in Home Assistant',
      type: 'toggle',
      default: true,
      help: 'Stays in the sidebar until dismissed, which is handy for something you fix on a weekend.',
    },
  ],
  build: (v: RecipeValues) =>
    joinLines([
      header('Blink – camera battery low', 'Notifies when a Blink camera transitions to a low battery state.'),
      'triggers:',
      '  - trigger: event',
      `    event_type: ${BATTERY_LOW_EVENT}`,
      conditionsBlock([cameraCondition(listValue(v, 'cameras'))]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '🔋 Blink camera battery low',
        '{{ trigger.event.data.camera }} is reporting a low battery{% if trigger.event.data.battery_voltage %} ({{ trigger.event.data.battery_voltage }} mV){% endif %}.',
      ),
      boolValue(v, 'persistent') ? '  - action: persistent_notification.create' : '',
      boolValue(v, 'persistent') ? '    data:' : '',
      boolValue(v, 'persistent') ? '      title: "Blink camera battery low"' : '',
      boolValue(v, 'persistent')
        ? `      message: ${yamlTemplate('Replace the batteries in {{ trigger.event.data.camera }}.', 6)}`
        : '',
      boolValue(v, 'persistent')
        ? `      notification_id: ${yamlTemplate('blink_battery_{{ trigger.event.data.camera | slugify }}', 6)}`
        : '',
    ]),
}

const downloaderStalled: Recipe = {
  id: 'downloader-stalled',
  name: 'Nothing has downloaded in a while',
  group: 'Maintenance',
  icon: '🕳️',
  description:
    'The failure nobody notices: Blink auth expired, the add-on stopped, or the cameras went offline, and clips quietly stopped arriving.',
  target: 'automations.yaml',
  filename: 'blink-downloader-stalled.yaml',
  fields: [
    {
      key: 'hours',
      label: 'Alert after',
      type: 'number',
      default: 12,
      min: 1,
      max: 168,
      suffix: 'hours',
      help: 'Set this comfortably longer than your quietest camera normally goes between clips.',
    },
    notifyField(),
  ],
  build: (v: RecipeValues) => {
    const hours = numberValue(v, 'hours', 12)
    return joinLines([
      header(
        `Blink – no clips downloaded in ${hours}h`,
        'Notifies when the add-on has not recorded a download for longer than expected.',
      ),
      'triggers:',
      '  - trigger: template',
      `    value_template: ${yamlTemplate(
        `{{ state_attr('${STATUS_SENSOR}', 'last_download') is not none and (as_timestamp(now()) - as_timestamp(state_attr('${STATUS_SENSOR}', 'last_download'), 0)) > ${hours * 3600} }}`,
        4,
      )}`,
      conditionsBlock([]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '🕳️ Blink has gone quiet',
        `No clips for over ${hours} hours. Last download: {{ state_attr('${STATUS_SENSOR}', 'last_download') }}. Check the add-on log and whether Blink needs re-authenticating.`,
      ),
    ])
  },
}

const dailySummary: Recipe = {
  id: 'daily-summary',
  name: 'Daily summary',
  group: 'Maintenance',
  icon: '📅',
  description: 'One message a day with clip counts and both storage levels.',
  target: 'automations.yaml',
  filename: 'blink-daily-summary.yaml',
  fields: [
    {
      key: 'at',
      label: 'Send at',
      type: 'time',
      default: '08:00',
    },
    notifyField(),
  ],
  build: (v: RecipeValues) =>
    joinLines([
      header('Blink – daily summary', 'A once-a-day digest of clip counts and storage levels.'),
      'triggers:',
      '  - trigger: time',
      `    at: ${yamlString(timeOfDay(stringValue(v, 'at', '08:00')))}`,
      conditionsBlock([]),
      'actions:',
      notifyAction(
        stringValue(v, 'notify_service', 'notify.notify'),
        '📅 Blink daily summary',
        `{{ states('${STATUS_SENSOR}') }} clips downloaded in total, {{ state_attr('${STATUS_SENSOR}', 'session_downloads') }} since the add-on last started. Local storage {{ states('${LOCAL_STORAGE_SENSOR}') }}%, cloud backup {{ states('${CLOUD_STORAGE_SENSOR}') }}%.`,
      ),
    ]),
}

export const AUTOMATION_RECIPES: Recipe[] = [
  suspiciousAlert,
  securityLights,
  castFeed,
  announceClip,
  newClipNotify,
  longClip,
  cloudStorageThreshold,
  localStorageThreshold,
  uploadBacklog,
  batteryLow,
  downloaderStalled,
  dailySummary,
]
