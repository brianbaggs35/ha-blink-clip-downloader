import { describe, expect, it } from 'vitest'
import {
  cameraCondition,
  conditionsBlock,
  eventNumberCondition,
  forDuration,
  notifyAction,
  pauseSwitchCondition,
  sourceCondition,
  timeWindowCondition,
} from './shared'

describe('notifyAction', () => {
  it('renders a plain notify action', () => {
    expect(notifyAction('notify.notify', 'Title', '{{ x }}')).toBe(
      ['  - action: notify.notify', '    data:', '      title: "Title"', '      message: >-', '        {{ x }}'].join(
        '\n',
      ),
    )
  })

  it('adds both platforms keys for a high-priority alert', () => {
    const yaml = notifyAction('notify.mobile_app_x', 'T', 'M', { critical: true })
    // iOS reads the first, Android the second; each ignores the other's.
    expect(yaml).toContain('critical: 1')
    expect(yaml).toContain('channel: alarm')
  })

  it("attaches the camera's image through Home Assistant's own proxy", () => {
    // Not the add-on's port: the phone is usually somewhere else entirely,
    // but it already reaches Home Assistant.
    const yaml = notifyAction('notify.x', 'T', 'M', { snapshot: true })
    expect(yaml).toContain('image: >-')
    expect(yaml).toContain('/api/camera_proxy/camera.blink_{{ trigger.event.data.camera | slugify }}')
  })

  it('sets both platforms tap-target keys', () => {
    const yaml = notifyAction('notify.x', 'T', 'M', { clickPath: '/hassio/ingress/abc' })
    expect(yaml).toContain('url: "/hassio/ingress/abc"')
    expect(yaml).toContain('clickAction: "/hassio/ingress/abc"')
  })

  it('emits no inner data block at all when no extras are asked for', () => {
    const yaml = notifyAction('notify.x', 'T', 'M', { clickPath: '' })
    expect(yaml.match(/data:/g)).toHaveLength(1)
  })

  it('puts every extra under the one inner data block', () => {
    const yaml = notifyAction('notify.x', 'T', 'M', {
      critical: true,
      snapshot: true,
      clickPath: '/lovelace/blink',
    })
    expect(yaml.match(/data:/g)).toHaveLength(2)
  })
})

describe('conditions', () => {
  it('cameraCondition is omitted entirely for an empty list', () => {
    expect(cameraCondition([])).toBe('')
    expect(cameraCondition(['Front Door'])).toContain('["Front Door"]')
  })

  it('sourceCondition is omitted entirely for an empty list', () => {
    expect(sourceCondition([])).toBe('')
    expect(sourceCondition(['pir'])).toContain('trigger.event.data.source in ["pir"]')
  })

  it('eventNumberCondition is omitted at zero, since zero accepts everything', () => {
    expect(eventNumberCondition('confidence', 0)).toBe('')
    expect(eventNumberCondition('confidence', 0.6)).toContain('(trigger.event.data.confidence | float(0)) >= 0.6')
  })

  it('timeWindowCondition renders whichever end is set', () => {
    expect(timeWindowCondition('', '')).toBe('')
    expect(timeWindowCondition('07:00:00', '')).toBe(['  - condition: time', '    after: "07:00:00"'].join('\n'))
    expect(timeWindowCondition('', '22:00:00')).toBe(['  - condition: time', '    before: "22:00:00"'].join('\n'))
    expect(timeWindowCondition('07:00:00', '22:00:00')).toContain('before: "22:00:00"')
  })

  it('pauseSwitchCondition requires the helper to be off', () => {
    expect(pauseSwitchCondition('')).toBe('')
    expect(pauseSwitchCondition('input_boolean.paused')).toContain('state: "off"')
  })

  it('conditionsBlock renders an explicit empty list when nothing applies', () => {
    expect(conditionsBlock([])).toBe('conditions: []')
    expect(conditionsBlock(['', ''])).toBe('conditions: []')
    expect(conditionsBlock(['  - condition: state'])).toBe('conditions:\n  - condition: state')
  })
})

describe('forDuration', () => {
  it('is omitted at zero and indents under the trigger otherwise', () => {
    expect(forDuration(0)).toBe('')
    expect(forDuration(15)).toBe('    for: "00:15:00"')
    expect(forDuration(15, 6)).toBe('      for: "00:15:00"')
  })
})
