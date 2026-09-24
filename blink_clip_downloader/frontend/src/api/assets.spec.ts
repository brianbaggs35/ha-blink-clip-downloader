import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { assetSnapshotUrl, createAsset, deleteAsset, getAssetActivity, listAssets, updateAsset } from './assets'
import type { AssetDraft } from './types'

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as Response
}

const DRAFT: AssetDraft = {
  name: 'Front door',
  asset_type: 'door',
  description: '',
  zone: { shape: 'rect', x_min: 0.1, y_min: 0.2, x_max: 0.3, y_max: 0.8 },
}

describe('assets api', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    vi.mocked(fetch).mockResolvedValue(jsonResponse({}))
  })
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('listAssets()', async () => {
    await listAssets()
    expect(fetch).toHaveBeenCalledWith('/api/assets', {})
  })

  it('createAsset() with the frame it was drawn on', async () => {
    await createAsset('Porch', DRAFT, 'clip-1')
    expect(fetch).toHaveBeenCalledWith('/api/assets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera: 'Porch', ...DRAFT, clip_id: 'clip-1' }),
    })
  })

  it("createAsset() on the camera's saved frame sends no clip", async () => {
    await createAsset('Porch', DRAFT)
    expect(fetch).toHaveBeenCalledWith('/api/assets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera: 'Porch', ...DRAFT }),
    })
  })

  it('updateAsset() encodes the id', async () => {
    await updateAsset('a/b', { enabled: false })
    expect(fetch).toHaveBeenCalledWith('/api/assets/a%2Fb', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false }),
    })
  })

  it('deleteAsset()', async () => {
    await deleteAsset('abc')
    expect(fetch).toHaveBeenCalledWith('/api/assets/abc', { method: 'DELETE' })
  })

  it('getAssetActivity()', async () => {
    await getAssetActivity()
    expect(fetch).toHaveBeenCalledWith('/api/assets/activity?days=7', {})
    await getAssetActivity(30)
    expect(fetch).toHaveBeenCalledWith('/api/assets/activity?days=30', {})
  })

  it('assetSnapshotUrl() encodes the camera', () => {
    expect(assetSnapshotUrl('Front Door')).toBe('/api/assets/snapshot/Front%20Door')
  })
})
