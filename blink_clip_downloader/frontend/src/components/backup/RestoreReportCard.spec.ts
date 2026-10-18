import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import RestoreReportCard from './RestoreReportCard.vue'
import { breakdown, report, row } from './testing'
import type { RestoreReport } from '../../api/types'

function mountCard(r: RestoreReport = report()) {
  return mount(RestoreReportCard, { props: { report: r, clipFolder: '/share/blink-clips' } })
}

describe('RestoreReportCard', () => {
  it('says the restore is complete and verified, and how much was added', () => {
    const wrapper = mountCard()
    expect(wrapper.find('.report-verdict').text()).toBe('Restore complete and verified — 180 records added.')
  })

  it('lists each check with whether it passed', () => {
    const items = mountCard().findAll('.check-list li')
    expect(items).toHaveLength(2)
    expect(items[0].find('.pi-check-circle').exists()).toBe(true)
    expect(items[0].text()).toContain('Every record in the backup was restored or deliberately left alone')
  })

  it('does not call it verified when a check failed, and reassures', () => {
    const wrapper = mountCard(
      report({
        verified: false,
        checks: [{ label: 'All 5 added clips are in the library', ok: false, detail: '2 are missing' }],
      }),
    )
    expect(wrapper.find('.report-verdict').text()).toContain('a check below did not pass')
    expect(wrapper.find('.report-verdict').text()).toContain('adds what is still missing')
    const failed = wrapper.find('.check-list li.failed')
    expect(failed.find('.pi-times-circle').exists()).toBe(true)
    expect(failed.text()).toContain('2 are missing')
  })

  it('tabulates what was added and what was already there', () => {
    const clips = mountCard()
      .findAll('tbody tr')
      .find((r) => r.attributes('data-table') === 'clips')!
    expect(clips.findAll('th, td').map((c) => c.text())).toEqual(['Clips', '100', '20'])
  })

  it('leaves out a table with nothing in it', () => {
    const wrapper = mountCard(report({ tables: [row('clips', 3, 3), row('security_events', 0, 0)] }))
    expect(wrapper.find('[data-table="security_events"]').exists()).toBe(false)
  })

  it('explains the clips that came back without a video', () => {
    expect(mountCard().find('.clip-notes').text()).toContain('Copy the videos back to /share/blink-clips')
  })

  it('has no notes when there is nothing to explain', () => {
    expect(
      mountCard(report({ clips: breakdown() }))
        .find('.clip-notes')
        .exists(),
    ).toBe(false)
  })

  it('reports each file by what happened to it', () => {
    const wrapper = mountCard(
      report({
        files: [
          { label: 'Camera settings', status: 'restored', detail: 'Restored.' },
          { label: 'Download history', status: 'failed', detail: 'Permission denied' },
        ],
      }),
    )
    const items = wrapper.findAll('.file-list li')
    expect(items.map((i) => i.attributes('data-file-status'))).toEqual(['restored', 'failed'])
    expect(items[1].text()).toContain('Permission denied')
  })

  it('has no detail line for a file with nothing more to say', () => {
    const wrapper = mountCard(report({ files: [{ label: 'Camera settings', status: 'restored', detail: '' }] }))
    expect(wrapper.find('.file-detail').exists()).toBe(false)
  })

  it('shows no files section when none were involved', () => {
    expect(mountCard(report({ files: [] })).text()).not.toContain('Settings and files')
  })

  it('says to restart when settings were written, and where', () => {
    const note = mountCard().find('.restart-note').text()
    expect(note).toContain('Restart the add-on')
    expect(note).toContain('Settings → Add-ons → Blink Clip Downloader → Restart')
  })

  it('does not ask for a restart when no setting changed', () => {
    expect(
      mountCard(report({ restart_needed: false }))
        .find('.restart-note')
        .exists(),
    ).toBe(false)
  })
})
