import { describe, expect, it } from 'vitest'
import { load } from 'js-yaml'
import {
  type DashboardOptions,
  DEFAULT_DASHBOARD_OPTIONS,
  cameraEntityId,
  cameraSetupSheet,
  castScriptYaml,
  dashboardYaml,
  kioskUrl,
  snapshotUrl,
} from './dashboard'

function options(overrides: Partial<DashboardOptions> = {}): DashboardOptions {
  return { ...DEFAULT_DASHBOARD_OPTIONS, cameras: ['Front Door', 'Back Yard'], ...overrides }
}

describe('urls', () => {
  it('encodes a camera name with spaces into the snapshot url', () => {
    expect(snapshotUrl('http://ha.local:8099', 'Front Door')).toBe(
      'http://ha.local:8099/api/security-feed/snapshot/Front%20Door',
    )
  })

  it('tolerates a trailing slash and an empty url', () => {
    expect(snapshotUrl('http://ha.local:8099///', 'A')).toBe('http://ha.local:8099/api/security-feed/snapshot/A')
    expect(snapshotUrl('', 'A')).toContain('homeassistant.local:8099')
  })

  it('builds the kiosk url the iframe card embeds', () => {
    expect(kioskUrl('http://ha.local:8099')).toBe('http://ha.local:8099/?kiosk=1&tab=securityfeed')
  })

  it('predicts the entity id Home Assistant will create', () => {
    expect(cameraEntityId('Front Door')).toBe('camera.blink_front_door')
  })
})

describe('camera setup sheet', () => {
  it('lists a name, url and resulting entity per camera', () => {
    const sheet = cameraSetupSheet(options())
    expect(sheet).toContain('Name:             Blink Front Door')
    expect(sheet).toContain('/api/security-feed/snapshot/Back%20Yard')
    expect(sheet).toContain('camera.blink_back_yard')
  })

  it('says so rather than rendering an empty sheet with no cameras', () => {
    expect(cameraSetupSheet(options({ cameras: [] }))).toContain('<no cameras found>')
  })
})

describe('dashboard yaml', () => {
  it('is a valid lovelace view with one picture-entity per camera', () => {
    const parsed = load(dashboardYaml(options({ columns: 3 }))) as {
      views: { path: string; cards: { type: string; columns?: number; cards?: { entity: string }[] }[] }[]
    }
    const view = parsed.views[0]
    expect(view.path).toBe('security-feed')
    expect(view.cards[0].type).toBe('grid')
    expect(view.cards[0].columns).toBe(3)
    expect(view.cards[0].cards?.map((c) => c.entity)).toEqual(['camera.blink_front_door', 'camera.blink_back_yard'])
  })

  it('embeds this tab instead when iframe mode is chosen', () => {
    const parsed = load(dashboardYaml(options({ mode: 'iframe' }))) as {
      views: { cards: { type: string; url?: string }[] }[]
    }
    expect(parsed.views[0].cards[0]).toMatchObject({
      type: 'iframe',
      url: 'http://homeassistant.local:8099/?kiosk=1&tab=securityfeed',
    })
  })

  it('drops the storage and status cards when they are switched off', () => {
    const full = load(dashboardYaml(options())) as { views: { cards: unknown[] }[] }
    expect(full.views[0].cards).toHaveLength(3)

    const bare = load(dashboardYaml(options({ includeStorage: false, includeStatus: false }))) as {
      views: { cards: unknown[] }[]
    }
    expect(bare.views[0].cards).toHaveLength(1)
  })

  it('falls back to a placeholder camera rather than an empty grid', () => {
    expect(dashboardYaml(options({ cameras: [] }))).toContain('camera.blink_front_door')
  })

  it('falls back to the default title and path when they are cleared', () => {
    const parsed = load(dashboardYaml(options({ viewTitle: '', viewPath: '' }))) as {
      views: { title: string; path: string }[]
    }
    expect(parsed.views[0]).toMatchObject({ title: 'Blink Security', path: 'security-feed' })
  })
})

describe('cast script', () => {
  it('targets the given display and the view just generated', () => {
    const parsed = load(castScriptYaml(options({ viewPath: 'cams' }), 'media_player.hub')) as {
      blink_cast_cameras: { sequence: { data: Record<string, string> }[] }
    }
    expect(parsed.blink_cast_cameras.sequence[0].data).toMatchObject({
      entity_id: 'media_player.hub',
      view_path: 'cams',
    })
  })

  it('falls back to a placeholder display and the default view path', () => {
    const parsed = load(castScriptYaml(options({ viewPath: '' }), '')) as {
      blink_cast_cameras: { sequence: { data: Record<string, string> }[] }
    }
    expect(parsed.blink_cast_cameras.sequence[0].data).toMatchObject({
      entity_id: 'media_player.nest_hub',
      view_path: 'security-feed',
    })
  })
})
