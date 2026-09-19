import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import EntityReferenceCard from './EntityReferenceCard.vue'

describe('EntityReferenceCard', () => {
  it('documents every sensor and event the add-on publishes', () => {
    const text = mount(EntityReferenceCard).text()
    for (const entity of [
      'sensor.blink_downloader_status',
      'sensor.blink_local_storage',
      'sensor.blink_cloud_storage',
      'blink_clip_downloaded',
      'blink_clip_analyzed',
      'blink_camera_battery_low',
    ]) {
      expect(text).toContain(entity)
    }
  })

  it('says how often the storage sensors are written', () => {
    expect(mount(EntityReferenceCard).text()).toContain('once per poll cycle')
  })
})
