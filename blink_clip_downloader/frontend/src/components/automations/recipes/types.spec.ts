import { describe, expect, it } from 'vitest'
import { load } from 'js-yaml'
import {
  type Recipe,
  boolValue,
  defaultValues,
  duration,
  entityList,
  jinjaList,
  jinjaString,
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

  it('jinjaString quotes one value the same way jinjaList quotes its items', () => {
    expect(jinjaString('alarm_control_panel.home')).toBe('"alarm_control_panel.home"')
    expect(jinjaList(['x'])).toBe(`[${jinjaString('x')}]`)
  })

  it('jinjaString escapes the non-printables JSON.stringify leaves raw', () => {
    // JSON escapes everything below 0x20 and nothing above it, but a Jinja
    // expression lands inside a YAML *block* scalar, which rejects DEL and
    // the C1 range outright — so these need escaping that JSON alone will
    // not give them.
    expect(jinjaString('a\u007fb')).toBe(String.raw`"a\u007fb"`)
    expect(jinjaString('a\u009fb')).toBe(String.raw`"a\u009fb"`)
    expect(jinjaList(['a\u007fb'])).toBe(String.raw`["a\u007fb"]`)
  })

  it('a jinja list stays inside a block scalar the YAML parser accepts', () => {
    const yaml = `value: ${yamlTemplate(`{{ x in ${jinjaList(['Cam\u007f1', 'Cam 2'])} }}`, 0)}`
    // The escape survives as text — which is the point: the YAML parser
    // never sees a raw DEL, and Jinja resolves the escape later.
    expect(load(yaml)).toEqual({ value: String.raw`{{ x in ["Cam\u007f1", "Cam 2"] }}` })
  })

  it("jinjaString escapes the apostrophe that would otherwise close states('...')", () => {
    // The whole reason the helper exists: interpolated raw, this ends the
    // Jinja literal early and the template stops compiling.
    expect(jinjaString("it's")).toBe('"it\'s"')
  })

  it('jinjaString escapes control characters, which would end the YAML scalar too', () => {
    expect(jinjaString('a\nb')).toBe('"a\\nb"')
    expect(jinjaString('a\rb')).toBe('"a\\rb"')
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

describe('yamlString and control characters', () => {
  // A raw newline in a double-quoted YAML scalar folds to a space, and in
  // a mapping key it makes the rest of the line a sibling key — so the
  // document either changes meaning or stops parsing.
  it.each([
    ['line1\nline2', 'line1\nline2'],
    ['tab\there', 'tab\there'],
    ['cr\r\nlf', 'cr\r\nlf'],
    ['Cam #2', 'Cam #2'],
    ['say "hi"', 'say "hi"'],
    ['back\\slash', 'back\\slash'],
    // The rest of the C0 range has no letter spelling and is not legal raw
    // in a double-quoted scalar at all — the parser rejects the whole
    // document, not just the value, so these get a numeric \\xNN escape.
    ['bell\u0007here', 'bell\u0007here'],
    ['nul\u0000here', 'nul\u0000here'],
    ['esc\u001bhere', 'esc\u001bhere'],
    ['del\u007fhere', 'del\u007fhere'],
  ])('round-trips %o through a YAML parser unchanged', (input, expected) => {
    expect(load(`value: ${yamlString(input)}`)).toEqual({ value: expected })
  })

  it('spells the three YAML knows by letter, and the rest numerically', () => {
    expect(yamlString('a\nb')).toBe(String.raw`"a\nb"`)
    expect(yamlString('a\rb')).toBe(String.raw`"a\rb"`)
    expect(yamlString('a\tb')).toBe(String.raw`"a\tb"`)
    expect(yamlString('a\u0007b')).toBe(String.raw`"a\x07b"`)
    expect(yamlString('a\u0000b')).toBe(String.raw`"a\x00b"`)
  })

  it('stays a single key when used as one', () => {
    expect(load(`${yamlString('x\nalias: pwned')}: 1`)).toEqual({ 'x\nalias: pwned': 1 })
  })
})
