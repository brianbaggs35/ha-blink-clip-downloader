import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ToggleSwitch from 'primevue/toggleswitch'
import RestoreSummaryCard from './RestoreSummaryCard.vue'
import { breakdown, row, summary } from './testing'
import type { RestoreSummary } from '../../api/types'

function mountCard(s: RestoreSummary = summary(), replaceSettings = false) {
  return mount(RestoreSummaryCard, {
    props: {
      summary: s,
      replaceSettings,
      'onUpdate:replaceSettings': (value: boolean | undefined) => void value,
    },
  })
}

describe('RestoreSummaryCard', () => {
  it('says where the backup came from and that it was checked', () => {
    const text = mountCard().text()
    expect(text).toContain('by version 6.1.3')
    expect(text).toContain('All 14 parts checked against their checksums')
  })

  it('shows the date as the person reading it would write it', () => {
    expect(mountCard().text()).toContain(new Date('2026-10-18T12:00:00+00:00').toLocaleString())
  })

  it('falls back to the raw text for a date it cannot read', () => {
    expect(mountCard(summary({ created_at: 'last Tuesday' })).text()).toContain('last Tuesday')
  })

  it('lists what is in the backup against what will be added and what is already here', () => {
    const rows = mountCard().findAll('tbody tr')
    const clips = rows.find((r) => r.attributes('data-table') === 'clips')!
    expect(clips.findAll('th, td').map((c) => c.text())).toEqual(['Clips', '120', '100', '20'])
  })

  it('leaves out a table with nothing in the backup', () => {
    expect(mountCard().find('[data-table="detected_objects"]').exists()).toBe(false)
  })

  it('says so when the backup holds only files', () => {
    const wrapper = mountCard(summary({ tables: [row('clips', 0, 0)], clips: breakdown() }))
    expect(wrapper.text()).toContain('The backup holds no records, only files.')
  })

  it('explains why clips are skipped and what comes back without a video', () => {
    const text = mountCard().find('.clip-notes').text()
    expect(text).toContain('20 clips already in this library — left exactly as they are.')
    expect(text).toContain('100 clips with no video on this device')
    expect(text).toContain('/share/blink-clips')
  })

  it('promises nothing is overwritten, and says how much will be added', () => {
    const text = mountCard().find('.total-line').text()
    expect(text).toContain('180 records will be added, 100 clips of them.')
    expect(text).toContain('Nothing that is already here is overwritten or deleted.')
  })

  it('tells you when restoring would change nothing', () => {
    const wrapper = mountCard(
      summary({
        tables: [row('clips', 5, 0)],
        clips: breakdown({ already_here: 5 }),
        files: [{ label: 'Camera settings', status: 'exists', detail: '' }],
      }),
    )
    expect(wrapper.find('.nothing-to-add').exists()).toBe(true)
    expect(wrapper.find('.total-line').exists()).toBe(false)
  })

  it('does not say so while a file would still be written', () => {
    const wrapper = mountCard(summary({ tables: [row('clips', 5, 0)], clips: breakdown({ already_here: 5 }) }))
    expect(wrapper.find('.nothing-to-add').exists()).toBe(false)
  })

  it('labels each file by what will happen to it', () => {
    const statuses = mountCard()
      .findAll('.file-list li')
      .map((li) => [li.attributes('data-file-status'), li.text()])
    expect(statuses[0][0]).toBe('new')
    expect(statuses[0][1]).toContain('Camera settings')
    expect(statuses[0][1]).toContain('Will be restored')
    expect(statuses[1][0]).toBe('exists')
    expect(statuses[1][1]).toContain('Already here — kept')
  })

  it('offers to replace what is already here only when something is', async () => {
    const wrapper = mountCard()
    expect(wrapper.find('.replace-option').exists()).toBe(true)

    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', true)
    expect(wrapper.emitted('update:replaceSettings')).toEqual([[true]])

    const nothing = mountCard(summary({ files: [{ label: 'Camera settings', status: 'new', detail: '' }] }))
    expect(nothing.find('.replace-option').exists()).toBe(false)
  })

  it('shows no files section when the backup has no files', () => {
    expect(mountCard(summary({ files: [] })).text()).not.toContain('Settings and files')
  })

  it('reflects the replace choice it is given', () => {
    expect(mountCard(summary(), true).findComponent(ToggleSwitch).props('modelValue')).toBe(true)
  })

  it('says faces are included, and that they are not enrolled twice', () => {
    const wrapper = mountCard(
      summary({ tables: [row('clips', 1, 1, 'Clips'), row('face_enrollments', 3, 2, 'Enrolled faces')] }),
    )
    expect(wrapper.find('.faces-note').text()).toContain('2 faces will be added')
    expect(wrapper.find('.faces-note').text()).toContain('not enrolled twice')
  })

  it('says nothing about faces when the backup has none', () => {
    expect(mountCard().find('.faces-note').exists()).toBe(false)
  })
})
