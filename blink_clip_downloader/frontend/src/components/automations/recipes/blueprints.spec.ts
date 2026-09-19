import { describe, expect, it } from 'vitest'
import { CORE_SCHEMA, defineScalarTag, load } from 'js-yaml'
import { BLUEPRINTS, BLUEPRINT_INSTALL_STEPS } from './blueprints'

// Home Assistant's own `!input` tag, which a stock YAML parser rejects.
// Declaring it here is what lets these tests check the rest of the document
// really is well-formed YAML. `identify: () => false` makes it load-only —
// nothing here ever dumps YAML back out.
const inputTag = defineScalarTag('!input', {
  resolve: (source: string) => ({ input: source }),
  identify: () => false,
})
// CORE_SCHEMA is what load() uses by default, so this adds the one tag and
// changes nothing else about how the document is parsed.
const HA_SCHEMA = CORE_SCHEMA.withTags(inputTag)

describe('blueprints', () => {
  it.each(BLUEPRINTS.map((bp) => [bp.id, bp] as const))(
    '%s is a parseable automation blueprint whose inputs are all declared',
    (_id, bp) => {
      const parsed = load(bp.yaml, { schema: HA_SCHEMA }) as {
        blueprint: { domain: string; name: string; input: Record<string, unknown> }
        triggers: unknown[]
        actions: unknown[]
      }
      expect(parsed.blueprint.domain).toBe('automation')
      expect(parsed.blueprint.name).toContain('Blink')
      expect(parsed.triggers).toBeTruthy()
      expect(parsed.actions).toBeTruthy()

      // Every !input reference has to name a declared input, or Home
      // Assistant rejects the blueprint on load.
      const declared = Object.keys(parsed.blueprint.input)
      const referenced = [...bp.yaml.matchAll(/!input (\w+)/g)].map((m) => m[1])
      expect(referenced.length).toBeGreaterThan(0)
      for (const name of referenced) expect(declared).toContain(name)
    },
  )

  it('brings every input used inside a template through variables', () => {
    // `!input` cannot be read from inside a Jinja template directly — it has
    // to be assigned to a variable first. A blueprint that gets this wrong
    // silently evaluates the condition against an undefined name.
    for (const bp of BLUEPRINTS) {
      const parsed = load(bp.yaml, { schema: HA_SCHEMA }) as {
        variables?: Record<string, unknown>
      }
      const templates = [...bp.yaml.matchAll(/\{\{[^}]*\}\}/g)].map((m) => m[0]).join(' ')
      for (const name of Object.keys(parsed.variables ?? {})) {
        expect(templates).toContain(name)
      }
    }
  })

  it('offers a distinct filename per blueprint', () => {
    const names = BLUEPRINTS.map((bp) => bp.filename)
    expect(new Set(names).size).toBe(names.length)
    for (const name of names) expect(name.endsWith('.yaml')).toBe(true)
  })

  it('documents where the file goes and how to reload it', () => {
    expect(BLUEPRINT_INSTALL_STEPS.join(' ')).toContain('config/blueprints/automation')
    expect(BLUEPRINT_INSTALL_STEPS.length).toBeGreaterThan(1)
  })
})
