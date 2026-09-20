import { describe, expect, it } from 'vitest'
import {
  type Recipe,
  boolValue,
  defaultValues,
  duration,
  entityList,
  jinjaList,
  joinLines,
  listValue,
  numberValue,
  slugify,
  stringValue,
  timeOfDay,
  yamlString,
  yamlTemplate,
} from './types'

describe('value accessors', () => {
  it('numberValue coerces and falls back for anything unusable', () => {
    expect(numberValue({ a: 12 }, 'a')).toBe(12)
    expect(numberValue({ a: '7' }, 'a')).toBe(7)
    expect(numberValue({ a: 'not a number' }, 'a', 5)).toBe(5)
    expect(numberValue({}, 'missing', 3)).toBe(3)
  })

  it('stringValue trims and falls back for blank input', () => {
    expect(stringValue({ a: '  notify.foo  ' }, 'a')).toBe('notify.foo')
    expect(stringValue({ a: '   ' }, 'a', 'notify.notify')).toBe('notify.notify')
    expect(stringValue({ a: 12 }, 'a', 'fallback')).toBe('fallback')
  })

  it('boolValue is true only for a real boolean true', () => {
    expect(boolValue({ a: true }, 'a')).toBe(true)
    expect(boolValue({ a: 'true' }, 'a')).toBe(false)
    expect(boolValue({}, 'a')).toBe(false)
  })

  it('listValue keeps only non-empty strings', () => {
    expect(listValue({ a: ['Front Door', ''] }, 'a')).toEqual(['Front Door'])
    expect(listValue({ a: 'Front Door' }, 'a')).toEqual([])
  })

  it('entityList splits on commas and newlines', () => {
    expect(entityList({ a: 'light.a, light.b\nlight.c' }, 'a')).toEqual(['light.a', 'light.b', 'light.c'])
    expect(entityList({ a: '  ' }, 'a', 'light.porch')).toEqual(['light.porch'])
    expect(entityList({ a: ',,' }, 'a')).toEqual([])
  })
})

describe('defaultValues', () => {
  it('copies array defaults so two builders cannot share one list', () => {
    const recipe = {
      fields: [
        { key: 'cameras', label: 'C', type: 'multiselect', default: [] as string[] },
        { key: 'threshold', label: 'T', type: 'number', default: 80 },
      ],
    } as unknown as Recipe
    const a = defaultValues(recipe)
    const b = defaultValues(recipe)
    ;(a.cameras as string[]).push('Front Door')
    expect(b.cameras).toEqual([])
    expect(b.threshold).toBe(80)
  })
})

describe('yaml helpers', () => {
  it('yamlString escapes quotes and backslashes', () => {
    expect(yamlString('plain')).toBe('"plain"')
    expect(yamlString('say "hi"')).toBe('"say \\"hi\\""')
    expect(yamlString('back\\slash')).toBe('"back\\\\slash"')
  })

  it('yamlTemplate folds a template into an indented block scalar', () => {
    expect(yamlTemplate('{{ a }}\n{{ b }}', 4)).toBe('>-\n      {{ a }}\n      {{ b }}')
  })

  it('jinjaList quotes each entry for Jinja, not YAML', () => {
    expect(jinjaList(['Front Door', 'Side "Gate"'])).toBe('["Front Door", "Side \\"Gate\\""]')
  })

  it('duration renders minutes as HH:MM:SS', () => {
    expect(duration(15)).toBe('00:15:00')
    expect(duration(90)).toBe('01:30:00')
    expect(duration(-5)).toBe('00:00:00')
  })

  it('timeOfDay normalizes and falls back for half-typed input', () => {
    expect(timeOfDay('8:05')).toBe('08:05:00')
    expect(timeOfDay('22:30:15')).toBe('22:30:15')
    expect(timeOfDay('99:99')).toBe('23:59:00')
    expect(timeOfDay('__:__')).toBe('08:00:00')
    // An empty fallback is what "no time limit" fields pass.
    expect(timeOfDay('', '')).toBe('')
  })

  it('joinLines drops omitted fragments but keeps deliberate blank lines', () => {
    expect(joinLines(['a', '', false, null, undefined, '\nb'])).toBe('a\n\nb')
  })

  it('slugify matches how Home Assistant names an entity', () => {
    expect(slugify('Front Door')).toBe('front_door')
    expect(slugify("Mom's Car!")).toBe('mom_s_car')
    expect(slugify('***')).toBe('camera')
  })
})
