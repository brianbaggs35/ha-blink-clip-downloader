import { describe, expect, it } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import BackupPage from './BackupPage.vue'
import BackupCreateSection from './BackupCreateSection.vue'
import RestoreSection from './RestoreSection.vue'

describe('BackupPage', () => {
  it('has a heading, a way to back up and a way to restore', () => {
    const wrapper = shallowMount(BackupPage)
    expect(wrapper.find('h2').text()).toBe('Backup & Restore')
    expect(wrapper.findAll('h3').map((h) => h.text())).toEqual(['Back up', 'Restore'])
    expect(wrapper.findComponent(BackupCreateSection).exists()).toBe(true)
    expect(wrapper.findComponent(RestoreSection).exists()).toBe(true)
  })

  it('tells you to keep the file away from this device', () => {
    expect(shallowMount(BackupPage).find('.page-intro').text()).toContain('somewhere other than this device')
  })
})
