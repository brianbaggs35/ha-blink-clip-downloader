import { describe, expect, it } from 'vitest'
import { AppTheme } from './theme'

// definePreset()'s return type is intentionally loose (it merges an
// arbitrary override object into Aura's preset), so this file's own custom
// tokens aren't reflected in its TS type — cast once here rather than
// sprinkling `as any` through the assertions below.
interface PresetShape {
  semantic: {
    primary: Record<string, string>
    colorScheme: { dark: { surface: Record<string, string> }; light: { surface: Record<string, string> } }
  }
}
const theme = AppTheme as unknown as PresetShape

describe('AppTheme', () => {
  it('overrides the primary palette with the violet accent scale', () => {
    expect(theme.semantic.primary[500]).toBe('#7c5cff')
    expect(theme.semantic.primary[950]).toBe('#241659')
  })

  it('overrides dark/light surface palettes', () => {
    expect(theme.semantic.colorScheme.dark.surface[950]).toBe('#08090f')
    expect(theme.semantic.colorScheme.light.surface[0]).toBe('#ffffff')
  })
})
