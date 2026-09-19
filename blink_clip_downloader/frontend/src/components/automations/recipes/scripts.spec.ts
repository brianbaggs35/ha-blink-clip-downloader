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
    else if (field.type === 'select')
      values[field.key] = field.options?.[field.options.length - 1]?.value ?? ''
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
    expect(notified).toContain('action: notify.x')
    expect(notified).not.toContain('tts.speak')
    expect(notified).not.toContain('media_player_entity_id')
  })
})

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
