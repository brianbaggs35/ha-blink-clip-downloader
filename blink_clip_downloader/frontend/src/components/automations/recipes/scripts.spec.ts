import { describe, expect, it } from 'vitest'
import { load } from 'js-yaml'
import { SCRIPT_RECIPES } from './scripts'
import { type Recipe, type RecipeValues, defaultValues } from './types'

function recipe(id: string): Recipe {
  const found = SCRIPT_RECIPES.find((r) => r.id === id)
  if (!found) throw new Error(`no recipe ${id}`)
  return found
}

function build(id: string, overrides: RecipeValues = {}): string {
  const r = recipe(id)
  return r.build({ ...defaultValues(r), ...overrides })
}

function flipped(r: Recipe): RecipeValues {
  const values = defaultValues(r)
  for (const field of r.fields) {
    if (field.type === 'toggle') values[field.key] = !field.default
    else if (field.type === 'number') values[field.key] = 0
    else if (field.type === 'select') values[field.key] = field.options?.[field.options.length - 1]?.value ?? ''
    else values[field.key] = ''
  }
  return values
}

describe('every script/scene/helper recipe', () => {
  it.each(SCRIPT_RECIPES.map((r) => [r.id, r] as const))(
    '%s produces parseable YAML with defaults and with everything flipped',
    (_id, r) => {
      for (const values of [defaultValues(r), flipped(r)]) {
        const yaml = r.build(values)
        expect(load(yaml)).toBeTruthy()
        expect(yaml).not.toContain('undefined')
        expect(yaml).not.toContain('[object Object]')
      }
    },
  )

  it('says where each one belongs', () => {
    for (const r of SCRIPT_RECIPES) {
      expect(['configuration.yaml', 'scripts.yaml', 'scenes.yaml']).toContain(r.target)
    }
  })
})

describe('sync now', () => {
  it('points the rest_command at the add-on URL given, without a double slash', () => {
    const parsed = load(build('script-sync-now', { addon_url: 'http://blink.local:8099/' })) as {
      rest_command: { blink_sync_now: { url: string; method: string } }
    }
    expect(parsed.rest_command.blink_sync_now.url).toBe('http://blink.local:8099/api/download-now')
    expect(parsed.rest_command.blink_sync_now.method).toBe('post')
  })

  it('only adds the confirmation notification when asked', () => {
    expect(build('script-sync-now', { confirm: true })).toContain('persistent_notification.create')
    expect(build('script-sync-now', { confirm: false })).not.toContain('persistent_notification')
  })
})

describe('cast script', () => {
  it('stops casting after the chosen delay, or not at all', () => {
    expect(build('script-cast-feed', { stop_after: 10 })).toContain('delay: "00:10:00"')
    expect(build('script-cast-feed', { stop_after: 0 })).not.toContain('media_player.turn_off')
  })
})

describe('storage report', () => {
  it('speaks it or notifies it, but never both', () => {
    const spoken = build('script-storage-report', { mode: 'tts' })
    expect(spoken).toContain('tts.speak')
    expect(spoken).toContain('media_player_entity_id')

    const notified = build('script-storage-report', { mode: 'notify', notify_service: 'notify.x' })
    const parsed = load(notified) as { blink_storage_report: { sequence: { action: string }[] } }
    expect(parsed.blink_storage_report.sequence[0].action).toBe('notify.x')
    expect(notified).not.toContain('tts.speak')
    expect(notified).not.toContain('media_player_entity_id')
  })
})

/** The notify actions a snapshot script emits, read from the parsed YAML
 * rather than matched in its text. */
function snapshotActions(yaml: string): { action: string }[] {
  const parsed = load(yaml) as { blink_camera_snapshots: { sequence: { action: string }[] } }
  return parsed.blink_camera_snapshots.sequence.filter((step) => step.action === 'notify.notify')
}

describe('security scene', () => {
  it('is a scenes.yaml list entry with each light set', () => {
    const parsed = load(
      build('scene-security-alert', { lights: 'light.a, light.b', brightness: 50, color: 'red' }),
    ) as { id: string; entities: Record<string, Record<string, unknown>> }[]
    expect(parsed[0].id).toBe('blink_security_alert')
    expect(Object.keys(parsed[0].entities)).toEqual(['light.a', 'light.b'])
    expect(parsed[0].entities['light.a']).toMatchObject({ state: 'on', brightness: 128 })
    expect(parsed[0].entities['light.a'].rgb_color).toEqual([255, 40, 40])
  })

  it('leaves the colour alone when that option is chosen', () => {
    expect(build('scene-security-alert', { color: 'none' })).not.toContain('rgb_color')
  })
})

describe('pause helper', () => {
  it('generates the helper and, by default, the automation that un-pauses it', () => {
    const withResume = build('helper-pause-alerts', { auto_resume: 30 })
    expect(withResume).toContain('input_boolean:')
    expect(withResume).toContain('for: "00:30:00"')
    expect(withResume).toContain('input_boolean.turn_off')
  })

  it('omits the un-pause automation when set to stay off indefinitely', () => {
    const forever = build('helper-pause-alerts', { auto_resume: 0 })
    expect(forever).toContain('input_boolean:')
    expect(forever).not.toContain('input_boolean.turn_off')
  })
})

describe('template sensors', () => {
  it('uses the chosen warning and critical levels', () => {
    const yaml = build('helper-template-sensors', { warning: 70, critical: 90 })
    expect(yaml).toContain("'critical' if worst >= 90")
    expect(yaml).toContain("'warning' if worst >= 70")
    const parsed = load(yaml) as { template: Record<string, unknown>[] }
    expect(parsed.template).toHaveLength(2)
  })
})

describe('snapshot script', () => {
  it('builds one message per selected camera', () => {
    const yaml = build('script-snapshot-all', { cameras: ['Front Door', 'Back Yard'] })
    expect(yaml).toContain('camera.blink_front_door')
    expect(yaml).toContain('camera.blink_back_yard')
    expect(snapshotActions(yaml)).toHaveLength(2)
  })

  it('falls back to a single placeholder camera when none are picked', () => {
    // It cannot mean "all" — build() never sees the live camera list.
    const yaml = build('script-snapshot-all', { cameras: [] })
    expect(snapshotActions(yaml)).toHaveLength(1)
  })
})

describe('what can be created directly in Home Assistant', () => {
  it('only offers it for recipes the config API can actually create', () => {
    const creatable = SCRIPT_RECIPES.filter((r) => r.create).map((r) => r.id)
    // The ones left out cannot be: sync-now, arm-sync and archive-now all
    // generate a rest_command too, the pause helper an input_boolean, and
    // the template sensors live in configuration.yaml. No API creates any
    // of those, so they stay copy-only.
    expect(creatable).toEqual([
      'script-cast-feed',
      'script-storage-report',
      'script-snapshot-all',
      'scene-security-alert',
      'scene-all-clear',
    ])
  })

  it('names each script with the same id its YAML nests the body under', () => {
    // The object id goes in the URL and becomes the entity id, while the
    // YAML key is stripped before sending. If they drifted apart, the
    // created script would not be the one the preview describes.
    for (const r of SCRIPT_RECIPES) {
      if (r.create?.kind !== 'script') continue
      const yaml = r.build(defaultValues(r))
      expect(yaml).toContain(`${r.create.objectId}:`)
    }
  })

  it('gives every scene the id its YAML declares', () => {
    const scenes = SCRIPT_RECIPES.filter((r) => r.create?.kind === 'scene')
    expect(scenes.length).toBeGreaterThan(1)
    for (const scene of scenes) {
      expect(scene.build(defaultValues(scene))).toContain(`- id: ${scene.create!.objectId}`)
    }
  })
})

describe('entity fields a user can mistype', () => {
  const awkward = ['notify: mobile_app_x', '{{ my_target }}', 'light.a#b', '*alias', '@thing']

  it.each(awkward)('still generates parseable YAML for a value of %o', (value) => {
    for (const r of SCRIPT_RECIPES) {
      const keys = r.fields.filter((f) => f.type === 'text').map((f) => f.key)
      for (const key of keys) {
        const yaml = r.build({ ...defaultValues(r), [key]: value })
        expect(() => load(yaml), `${r.id}.${key}`).not.toThrow()
      }
    }
  })
})
