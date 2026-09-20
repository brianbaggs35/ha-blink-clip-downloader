/** The recipe model behind the Automations tab's builders.
 *
 * A recipe is a small description of one thing a user might want Home
 * Assistant to do, plus the fields that make it *theirs* (a threshold, a
 * notify service, which cameras) and a pure function turning those values
 * into YAML. Keeping the catalogue as data means the builder UI is written
 * once and every new automation, script or scene is a few lines here rather
 * than another hand-written snippet in the page template — which is what the
 * old static tab had become.
 */

export type FieldValue = string | number | boolean | string[]

export type RecipeValues = Record<string, FieldValue>

export type FieldType = 'number' | 'text' | 'select' | 'multiselect' | 'toggle' | 'time'

export interface RecipeField {
  key: string
  label: string
  type: FieldType
  /** One line under the input. Keep it about *why*, not what. */
  help?: string
  default: FieldValue
  options?: { label: string; value: string }[]
  min?: number
  max?: number
  step?: number
  suffix?: string
  placeholder?: string
  /** Fill `options` at runtime from the live camera list. */
  source?: 'cameras'
}

/** What Home Assistant's config API can create directly, and under which
 * object id. Absent means copy-only: blueprints, helpers, dashboards and
 * anything that belongs in configuration.yaml have no API to create them
 * (see blink_downloader/ha_config.py), and the builder says where to put
 * them by hand instead. */
export interface RecipeCreate {
  kind: 'automation' | 'script' | 'scene'
  /** Stable, so pressing Create twice updates rather than duplicates. For
   * a script it must match the id the generated YAML nests its body under,
   * or the created entity would not be the one the YAML describes. */
  objectId: string
}

export interface Recipe {
  id: string
  name: string
  /** Section heading in the picker. */
  group: string
  icon: string
  description: string
  /** Where the generated YAML belongs, shown above the preview. */
  target: string
  /** Offered as the filename when the YAML is downloaded. */
  filename: string
  fields: RecipeField[]
  build: (values: RecipeValues) => string
  create?: RecipeCreate
}

// ---------------------------------------------------------------------------
// Value accessors
//
// Every field's value arrives as a FieldValue union because one object holds
// the whole form. These narrow it *and* apply the recipe's own fallback, so a
// build() never has to care whether a field was cleared to null by an input
// component mid-edit — the preview keeps rendering valid YAML while someone
// is still typing, which is the whole point of a live preview.
// ---------------------------------------------------------------------------

export function numberValue(values: RecipeValues, key: string, fallback = 0): number {
  const raw = values[key]
  const n = typeof raw === 'number' ? raw : Number(raw)
  return Number.isFinite(n) ? n : fallback
}

export function stringValue(values: RecipeValues, key: string, fallback = ''): string {
  const raw = values[key]
  if (typeof raw === 'string' && raw.trim()) return raw.trim()
  return fallback
}

export function boolValue(values: RecipeValues, key: string): boolean {
  return values[key] === true
}

export function listValue(values: RecipeValues, key: string): string[] {
  const raw = values[key]
  if (Array.isArray(raw)) return raw.filter((v) => typeof v === 'string' && v.length > 0)
  return []
}

/** Split a comma/newline separated free-text field into entity ids. */
export function entityList(values: RecipeValues, key: string, fallback = ''): string[] {
  return stringValue(values, key, fallback)
    .split(/[\n,]+/)
    .map((part) => part.trim())
    .filter(Boolean)
}

export function defaultValues(recipe: Recipe): RecipeValues {
  const out: RecipeValues = {}
  for (const field of recipe.fields) {
    out[field.key] = Array.isArray(field.default) ? [...field.default] : field.default
  }
  return out
}

// ---------------------------------------------------------------------------
// YAML helpers
// ---------------------------------------------------------------------------

// One backslash and two, written without escaping an escape. A raw
// template cannot *end* in a backslash (it would escape the closing
// backtick), so the single one is taken from the head of the pair.
const ESCAPED_BACKSLASH = String.raw`\\`
const BACKSLASH = ESCAPED_BACKSLASH[0]
const ESCAPED_QUOTE = String.raw`\"`

/** A double-quoted YAML scalar — safe for text starting with an emoji, a
 * brace, or anything else a plain scalar would choke on.
 *
 * Control characters are escaped too, not just the quote and backslash: a
 * raw newline inside a double-quoted scalar is legal YAML but *folds* to a
 * space, so the value silently stops being what the user typed — and when
 * the scalar is a mapping key, the text after the newline is read as a
 * sibling key instead and the document usually stops parsing at all.
 * Reaching this needs a newline in a value, which a single-line <input>
 * will not produce; camera names arrive from Blink's API rather than from
 * an input, so this helper does not get to assume where its argument
 * came from.
 */
export function yamlString(value: string): string {
  const escaped = value
    .replaceAll(BACKSLASH, ESCAPED_BACKSLASH)
    .replaceAll('"', ESCAPED_QUOTE)
    .replaceAll('\n', String.raw`\n`)
    .replaceAll('\r', String.raw`\r`)
    .replaceAll('\t', String.raw`\t`)
  return `"${escaped}"`
}

/** A folded block scalar, which is how Jinja templates get into YAML without
 * quoting every brace. `>-` keeps it a single line once folded. */
export function yamlTemplate(template: string, indent: number): string {
  const pad = ' '.repeat(indent)
  const body = template
    .trim()
    .split('\n')
    .map((line) => `${pad}  ${line.trim()}`)
    .join('\n')
  return `>-\n${body}`
}

/** Render a list of strings as an inline Jinja list, quoted for Jinja (not
 * for YAML — the caller puts this inside a template). */
export function jinjaList(items: string[]): string {
  return `[${items.map((item) => JSON.stringify(item)).join(', ')}]`
}

/** Minutes as HH:MM:SS, which is what `for:`/`delay:` want. */
export function duration(minutes: number): string {
  const safe = Math.max(0, Math.round(minutes))
  const h = Math.floor(safe / 60)
  const m = safe % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:00`
}

/** Normalize a free-text time field to HH:MM:SS. */
export function timeOfDay(value: string, fallback = '08:00:00'): string {
  const match = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim())
  if (!match) return fallback
  const h = Math.min(23, Number(match[1]))
  const m = Math.min(59, Number(match[2]))
  const s = Math.min(59, Number(match[3] ?? '0'))
  return [h, m, s].map((part) => String(part).padStart(2, '0')).join(':')
}

/** Drop empty lines the builders leave behind when an optional block is
 * omitted, so the preview never shows a ragged gap. */
export function joinLines(parts: (string | false | null | undefined)[]): string {
  return parts.filter((part): part is string => Boolean(part)).join('\n')
}

/** Strip *char* from both ends of *value*.
 *
 * A loop rather than a regex: an anchored `+` over a repeated character
 * backtracks quadratically on a long run of it, which a camera name typed
 * by hand is entitled to contain.
 */
export function trimChar(value: string, char: string): string {
  let start = 0
  while (start < value.length && value[start] === char) start++
  let end = value.length
  while (end > start && value[end - 1] === char) end--
  return value.slice(start, end)
}

/** As {@link trimChar}, but only at the end — a URL's leading characters are
 * part of its scheme and must survive. */
export function trimTrailingChar(value: string, char: string): string {
  let end = value.length
  while (end > 0 && value[end - 1] === char) end--
  return value.slice(0, end)
}

/** An `entity_id`/`camera` slug of a free-text name, matching how Home
 * Assistant itself slugifies a friendly name. */
export function slugify(name: string): string {
  return trimChar(name.toLowerCase().replaceAll(/[^a-z0-9]+/g, '_'), '_') || 'camera'
}
