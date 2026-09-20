import { afterEach, describe, expect, it, vi } from 'vitest'
import { createInHomeAssistant } from './haConfig'

describe('createInHomeAssistant', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('posts the kind, id and YAML', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ created: true, entity_id: 'automation.blink_x' }),
    })
    vi.stubGlobal('fetch', fetchMock)

    const result = await createInHomeAssistant('automation', 'blink_x', 'alias: x')

    expect(fetchMock).toHaveBeenCalledWith('/api/ha/config/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'automation', object_id: 'blink_x', yaml: 'alias: x' }),
    })
    expect(result.entity_id).toBe('automation.blink_x')
  })
})
