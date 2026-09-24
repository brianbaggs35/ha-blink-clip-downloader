import { describe, expect, it } from 'vitest'
import { ASSET_TYPES, ZONE_COLORS, assetTypeInfo, zoneColor } from './assetTypes'

describe('assetTypes', () => {
  it('covers every markable type once, each with an icon and what it is watched for', () => {
    const values = ASSET_TYPES.map((t) => t.value)
    expect(new Set(values).size).toBe(values.length)
    expect(values).toEqual([
      'door',
      'window',
      'garage',
      'gate',
      'package_area',
      'mailbox',
      'bicycle',
      'equipment',
      'other',
    ])
    for (const info of ASSET_TYPES) {
      expect(info.icon).toMatch(/^pi pi-/)
      expect(info.watchedFor.length).toBeGreaterThan(20)
    }
  })

  it('looks a type up, and falls back for one it does not know', () => {
    expect(assetTypeInfo('mailbox').label).toBe('Mailbox')
    expect(assetTypeInfo('hovercraft').value).toBe('other')
  })

  it('cycles through the zone colours, in either direction', () => {
    expect(zoneColor(0)).toBe(ZONE_COLORS[0])
    expect(zoneColor(ZONE_COLORS.length + 1)).toBe(ZONE_COLORS[1])
    expect(zoneColor(-1)).toBe(ZONE_COLORS[ZONE_COLORS.length - 1])
  })
})
