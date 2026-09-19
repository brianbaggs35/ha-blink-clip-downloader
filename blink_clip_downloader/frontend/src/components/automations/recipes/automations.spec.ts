import { describe, expect, it } from 'vitest'
import { load } from 'js-yaml'
import { AUTOMATION_RECIPES } from './automations'
import { type Recipe, type RecipeValues, defaultValues } from './types'

function recipe(id: string): Recipe {
  const found = AUTOMATION_RECIPES.find((r) => r.id === id)
  if (!found) throw new Error(`no recipe ${id}`)
  return found
}

function build(id: string, overrides: RecipeValues = {}): string {
  const r = recipe(id)
  return r.build({ ...defaultValues(r), ...overrides })
}

/** Flip every field away from its default, which is what walks the other
 * side of each build()'s optional blocks. */
function flipped(r: Recipe): RecipeValues {
  const values = defaultValues(r)
  for (const field of r.fields) {
    if (field.type === 'toggle') values[field.key] = !field.default
    else if (field.type === 'number') values[field.key] = 0
    else if (field.type === 'multiselect')
      values[field.key] = field.source === 'cameras' ? ['Front Door', 'Back Yard'] : ['pir']
    else if (field.type === 'select')
      values[field.key] = field.options?.[field.options.length - 1]?.value ?? ''
    else if (field.type === 'time') values[field.key] = '23:30'
    else values[field.key] = field.key === 'pause_entity' ? 'input_boolean.paused' : ''
  }
  return values
}

describe('every automation recipe', () => {
  it.each(AUTOMATION_RECIPES.map((r) => [r.id, r] as const))(
    '%s produces YAML Home Assistant can parse, with defaults and with everything flipped',
    (_id, r) => {
      for (const values of [defaultValues(r), flipped(r)]) {
        const yaml = r.build(values)
        const parsed = load(yaml) as Record<string, unknown>
        expect(parsed).toBeTypeOf('object')
        // Modern HA syntax, which is what the UI editor writes back.
        expect(parsed).toHaveProperty('triggers')
        expect(parsed).toHaveProperty('actions')
        expect(parsed.alias).toBeTypeOf('string')
        expect(yaml).not.toContain('undefined')
        expect(yaml).not.toContain('[object Object]')
      }
    },
  )

  it('has a unique id, filename and non-empty field list for each entry', () => {
    const ids = AUTOMATION_RECIPES.map((r) => r.id)
    expect(new Set(ids).size).toBe(ids.length)
    const files = AUTOMATION_RECIPES.map((r) => r.filename)
    expect(new Set(files).size).toBe(files.length)
    for (const r of AUTOMATION_RECIPES) expect(r.fields.length).toBeGreaterThan(0)
  })
})

describe('cloud storage threshold', () => {
  it('triggers on the percentage the user chose, not a baked-in one', () => {
    const parsed = load(build('cloud-storage-threshold', { threshold: 42 })) as {
      triggers: { entity_id?: string; above?: number }[]
    }
    expect(parsed.triggers[0]).toMatchObject({
      entity_id: 'sensor.blink_cloud_storage',
      above: 42,
    })
  })

  it('pairs the daily reminder trigger with a condition, so it only fires while still full', () => {
    const on = load(build('cloud-storage-threshold', { remind_daily: true, remind_at: '09:30' })) as {
      triggers: { at?: string }[]
      conditions: unknown[]
    }
    expect(on.triggers).toHaveLength(2)
    expect(on.triggers[1].at).toBe('09:30:00')
    expect(on.conditions).toHaveLength(1)

    const off = load(build('cloud-storage-threshold', { remind_daily: false })) as {
      triggers: unknown[]
      conditions: unknown[]
    }
    expect(off.triggers).toHaveLength(1)
    expect(off.conditions).toEqual([])
  })

  it('drops the sustained-for clause when it is set to zero', () => {
    expect(build('cloud-storage-threshold', { sustained: 0 })).not.toContain('for:')
    expect(build('cloud-storage-threshold', { sustained: 30 })).toContain('for: "00:30:00"')
  })
})

describe('local storage threshold', () => {
  it('names the basis so "80% full" is not ambiguous', () => {
    expect(build('local-storage-threshold')).toContain("'basis'")
  })
})

describe('upload backlog', () => {
  it('watches the pending_uploads attribute rather than the state', () => {
    const parsed = load(build('cloud-upload-backlog', { pending: 5 })) as {
      triggers: { attribute?: string; above?: number }[]
    }
    expect(parsed.triggers[0]).toMatchObject({ attribute: 'pending_uploads', above: 5 })
  })
})

describe('suspicious clip alert', () => {
  it('requires the suspicious flag and both numeric floors', () => {
    const yaml = build('suspicious-clip-alert', { min_confidence: 0.8, min_risk: 40 })
    expect(yaml).toContain('{{ trigger.event.data.is_suspicious }}')
    expect(yaml).toContain('(trigger.event.data.confidence | float(0)) >= 0.8')
    expect(yaml).toContain('(trigger.event.data.risk_score | float(0)) >= 40')
  })

  it('drops the risk floor at zero rather than emitting a condition that is always true', () => {
    expect(build('suspicious-clip-alert', { min_risk: 0 })).not.toContain('risk_score | float(0)) >=')
  })

  it('limits to the chosen cameras', () => {
    expect(build('suspicious-clip-alert', { cameras: ['Front Door'] })).toContain(
      'trigger.event.data.camera in ["Front Door"]',
    )
  })

  it('queues rather than dropping a second clip that arrives mid-run', () => {
    const parsed = load(build('suspicious-clip-alert')) as { mode: string; max: number }
    expect(parsed.mode).toBe('queued')
    expect(parsed.max).toBe(10)
  })
})

describe('security lights', () => {
  it('brightens lights, switches switches, and turns everything off again', () => {
    // homeassistant.turn_on forwards its data to the domain's own service,
    // so a switch called with brightness_pct is rejected outright — lights
    // and everything else have to be separate actions.
    const parsed = load(build('security-lights', { lights: 'light.a, switch.b', auto_off: 5 })) as {
      actions: { action: string; target?: { entity_id: string[] }; data?: Record<string, number> }[]
    }
    expect(parsed.actions[0]).toMatchObject({ action: 'light.turn_on' })
    expect(parsed.actions[0].target?.entity_id).toEqual(['light.a'])
    expect(parsed.actions[0].data).toMatchObject({ brightness_pct: 100 })
    expect(parsed.actions[1]).toMatchObject({ action: 'homeassistant.turn_on' })
    expect(parsed.actions[1].target?.entity_id).toEqual(['switch.b'])
    expect(parsed.actions.at(-1)).toMatchObject({ action: 'homeassistant.turn_off' })
    expect(parsed.actions.at(-1)?.target?.entity_id).toEqual(['light.a', 'switch.b'])
  })

  it('emits no brightness action at all when only switches are listed', () => {
    const parsed = load(build('security-lights', { lights: 'switch.b', auto_off: 0 })) as {
      actions: { action: string }[]
    }
    expect(parsed.actions.map((a) => a.action)).toEqual(['homeassistant.turn_on'])
  })

  it('leaves the lights on when the auto-off delay is zero', () => {
    const parsed = load(build('security-lights', { auto_off: 0 })) as { actions: unknown[] }
    expect(parsed.actions).toHaveLength(1)
  })

  it('uses a sun condition rather than fixed times when only-after-dark is on', () => {
    expect(build('security-lights', { after_dark: true })).toContain('condition: sun')
    expect(build('security-lights', { after_dark: false })).not.toContain('condition: sun')
  })
})

describe('cast the feed', () => {
  it('casts the chosen view and stops again', () => {
    const parsed = load(
      build('cast-security-feed', { media_player: 'media_player.hub', stop_after: 2 }),
    ) as { actions: { action: string; data?: Record<string, string>; delay?: string }[] }
    expect(parsed.actions[0]).toMatchObject({ action: 'cast.show_lovelace_view' })
    expect(parsed.actions[0].data).toMatchObject({ entity_id: 'media_player.hub' })
    expect(parsed.actions[1].delay).toBe('00:02:00')
    expect(parsed.actions[2]).toMatchObject({ action: 'media_player.turn_off' })
  })

  it('leaves the feed up when the stop delay is zero', () => {
    const parsed = load(build('cast-security-feed', { stop_after: 0 })) as { actions: unknown[] }
    expect(parsed.actions).toHaveLength(1)
  })
})

describe('announce a clip', () => {
  it('listens to the analysis event when only suspicious clips should be announced', () => {
    const suspicious = load(build('announce-clip', { suspicious_only: true })) as {
      triggers: { event_type: string }[]
    }
    expect(suspicious.triggers[0].event_type).toBe('blink_clip_analyzed')

    const all = load(build('announce-clip', { suspicious_only: false })) as {
      triggers: { event_type: string }[]
    }
    expect(all.triggers[0].event_type).toBe('blink_clip_downloaded')
  })

  it('speaks through the chosen tts entity onto the chosen speaker', () => {
    const parsed = load(
      build('announce-clip', { tts_entity: 'tts.piper', media_player: 'media_player.office' }),
    ) as { actions: { action: string; target: { entity_id: string }; data: Record<string, string> }[] }
    expect(parsed.actions[0].action).toBe('tts.speak')
    expect(parsed.actions[0].target.entity_id).toBe('tts.piper')
    expect(parsed.actions[0].data.media_player_entity_id).toBe('media_player.office')
  })
})

describe('new clip notification', () => {
  it('has no time condition until a time is actually given', () => {
    expect(build('new-clip-notify')).not.toContain('condition: time')
    expect(build('new-clip-notify', { after: '07:00' })).toContain('after: "07:00:00"')
  })

  it('does not invent a window from a half-typed time', () => {
    // InputMask hands over "0_:__" mid-edit; inventing 08:00 from that would
    // silently drop every notification outside it.
    expect(build('new-clip-notify', { after: '0_:__' })).not.toContain('condition: time')
  })

  it('filters by source when sources are chosen', () => {
    expect(build('new-clip-notify', { sources: ['pir', 'liveview'] })).toContain(
      '["pir", "liveview"]',
    )
  })
})

describe('long clip', () => {
  it('compares against the chosen duration', () => {
    expect(build('long-motion-clip', { seconds: 45 })).toContain(
      '(trigger.event.data.duration | float(0)) > 45',
    )
  })
})

describe('battery low', () => {
  it('optionally leaves a persistent notification with a stable id per camera', () => {
    const withIt = load(build('camera-battery-low', { persistent: true })) as { actions: unknown[] }
    expect(withIt.actions).toHaveLength(2)
    expect(build('camera-battery-low', { persistent: true })).toContain('notification_id:')

    const without = load(build('camera-battery-low', { persistent: false })) as { actions: unknown[] }
    expect(without.actions).toHaveLength(1)
  })
})

describe('stalled downloader', () => {
  it('converts the chosen hours into the template threshold', () => {
    expect(build('downloader-stalled', { hours: 3 })).toContain('> 10800')
  })

  it('guards against a sensor that has never recorded a download', () => {
    expect(build('downloader-stalled')).toContain('is not none')
  })
})

describe('daily summary', () => {
  it('fires at the chosen time and reports both storage sensors', () => {
    const yaml = build('daily-summary', { at: '06:45' })
    expect(yaml).toContain('at: "06:45:00"')
    expect(yaml).toContain('sensor.blink_local_storage')
    expect(yaml).toContain('sensor.blink_cloud_storage')
  })
})
