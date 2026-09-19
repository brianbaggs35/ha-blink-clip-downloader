/** Home Assistant blueprints for this add-on.
 *
 * A blueprint is the right shape when the *same* automation gets set up more
 * than once — one per phone, one per camera group — because Home Assistant
 * then asks for the inputs in its own UI and keeps every copy in step with
 * the blueprint. The builder tabs cover the one-off case; these cover the
 * repeated one.
 *
 * Deliberately kept here rather than as files in the repo: a blueprint
 * imported by URL is pinned to whatever that URL served, while these are
 * copied or downloaded from the running add-on, so they always match the
 * version of the add-on that is actually installed.
 */

export interface BlueprintDoc {
  id: string
  name: string
  icon: string
  description: string
  filename: string
  yaml: string
}

const SUSPICIOUS = `blueprint:
  name: Blink – suspicious clip alert
  description: >-
    Notify when the Blink Clip Downloader add-on finishes analyzing a clip and
    judged it suspicious. Set it up once per person you want notified.
  domain: automation
  input:
    notify_service:
      name: Notify service
      description: "For example: notify.mobile_app_pixel"
      default: notify.notify
      selector:
        text:
    min_confidence:
      name: Minimum confidence
      description: How sure the model has to be before this notifies.
      default: 0.6
      selector:
        number:
          min: 0
          max: 1
          step: 0.05
          mode: slider
    min_risk:
      name: Minimum risk score
      description: >-
        From the deterministic security layer (0-100). Leave at 0 unless the
        object-detection pipeline is enabled.
      default: 0
      selector:
        number:
          min: 0
          max: 100
          mode: slider
    cameras:
      name: Cameras
      description: Comma separated camera names. Leave empty for every camera.
      default: ""
      selector:
        text:

mode: queued
max: 10

variables:
  min_confidence: !input min_confidence
  min_risk: !input min_risk
  cameras: !input cameras

triggers:
  - trigger: event
    event_type: blink_clip_analyzed

conditions:
  - condition: template
    value_template: "{{ trigger.event.data.is_suspicious }}"
  - condition: template
    value_template: >-
      {{ (trigger.event.data.confidence | float(0)) >= (min_confidence | float(0)) }}
  - condition: template
    value_template: >-
      {{ (trigger.event.data.risk_score | float(0)) >= (min_risk | float(0)) }}
  - condition: template
    value_template: >-
      {{ cameras | trim == '' or trigger.event.data.camera in (cameras.split(',') | map('trim') | list) }}

actions:
  - action: !input notify_service
    data:
      title: "🚨 Blink: {{ trigger.event.data.camera }}"
      message: >-
        {{ trigger.event.data.summary }} (confidence
        {{ (trigger.event.data.confidence | float(0) * 100) | round(0) }}%)
`

const STORAGE = `blueprint:
  name: Blink – storage watchdog
  description: >-
    Notify when one of the Blink Clip Downloader's storage sensors passes a
    percentage you choose. Set it up once for local storage and once for cloud
    backup.
  domain: automation
  input:
    storage_sensor:
      name: Storage sensor
      description: sensor.blink_local_storage or sensor.blink_cloud_storage.
      default: sensor.blink_cloud_storage
      selector:
        entity:
          filter:
            - domain: sensor
    threshold:
      name: Notify above
      default: 85
      selector:
        number:
          min: 1
          max: 99
          unit_of_measurement: "%"
          mode: slider
    sustained_minutes:
      name: Only after it stays there for
      default: 15
      selector:
        number:
          min: 0
          max: 720
          unit_of_measurement: min
          mode: box
    notify_service:
      name: Notify service
      default: notify.notify
      selector:
        text:

mode: single

variables:
  storage_sensor: !input storage_sensor

triggers:
  - trigger: numeric_state
    entity_id: !input storage_sensor
    above: !input threshold
    for:
      minutes: !input sustained_minutes

conditions: []

actions:
  - action: !input notify_service
    data:
      title: "💾 Blink storage is filling up"
      message: >-
        {{ state_attr(storage_sensor, 'friendly_name') }} is at
        {{ states(storage_sensor) }}%.
`

const BATTERY = `blueprint:
  name: Blink – camera battery low
  description: >-
    Notify the first time a Blink camera drops to a low battery state. The
    add-on only fires this on a genuine transition, so it does not repeat every
    poll cycle.
  domain: automation
  input:
    notify_service:
      name: Notify service
      default: notify.notify
      selector:
        text:
    cameras:
      name: Cameras
      description: Comma separated camera names. Leave empty for every camera.
      default: ""
      selector:
        text:

mode: queued
max: 10

variables:
  cameras: !input cameras

triggers:
  - trigger: event
    event_type: blink_camera_battery_low

conditions:
  - condition: template
    value_template: >-
      {{ cameras | trim == '' or trigger.event.data.camera in (cameras.split(',') | map('trim') | list) }}

actions:
  - action: !input notify_service
    data:
      title: "🔋 Blink camera battery low"
      message: >-
        {{ trigger.event.data.camera }} is reporting a low battery.
`

export const BLUEPRINTS: BlueprintDoc[] = [
  {
    id: 'suspicious',
    name: 'Suspicious clip alert',
    icon: '🚨',
    description:
      'Per-person alerting with its own confidence and risk thresholds — set it up once for each phone rather than editing a shared automation.',
    filename: 'blink_suspicious_clip_alert.yaml',
    yaml: SUSPICIOUS,
  },
  {
    id: 'storage',
    name: 'Storage watchdog',
    icon: '💾',
    description:
      'One blueprint covering both storage sensors: add it twice with different thresholds for local and cloud.',
    filename: 'blink_storage_watchdog.yaml',
    yaml: STORAGE,
  },
  {
    id: 'battery',
    name: 'Camera battery low',
    icon: '🔋',
    description: 'Battery alerts for whichever cameras you care about, delivered wherever you want them.',
    filename: 'blink_camera_battery_low.yaml',
    yaml: BATTERY,
  },
]

/** Where a blueprint file goes, and what to do after saving it. */
export const BLUEPRINT_INSTALL_STEPS = [
  'Save the file under config/blueprints/automation/blink_clip_downloader/ (create the folders if they do not exist).',
  'In Home Assistant, go to Settings → Automations & scenes → Blueprints and press the reload button in the three-dot menu.',
  'Press "Create automation" on the blueprint, fill in the inputs, and save. Repeat for each phone, camera group or threshold.',
]
