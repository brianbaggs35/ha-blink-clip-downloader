import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import FileUpload from 'primevue/fileupload'
import ProgressBar from 'primevue/progressbar'
import ToggleSwitch from 'primevue/toggleswitch'
import RestoreSection from './RestoreSection.vue'
import { report, row, summary } from './testing'
import { ApiError } from '../../api/client'
import type { RestoreReport, RestoreSummary } from '../../api/types'
import { discardBackupUpload, inspectBackup, restoreBackup, uploadBackupFile } from '../../api/backup'
import { useConfirmStore } from '../../stores/confirm'
import { useRefreshStore } from '../../stores/refresh'

vi.mock('../../api/backup', () => ({
  uploadBackupFile: vi.fn(),
  inspectBackup: vi.fn(),
  restoreBackup: vi.fn(),
  discardBackupUpload: vi.fn(),
}))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

type Wrapper = ReturnType<typeof mountSection>

function mountSection() {
  return mount(RestoreSection)
}

async function choose(wrapper: Wrapper, ...names: string[]) {
  const files = (names.length ? names : ['backup.zip']).map(
    (name) => new File(['x'], name, { type: 'application/zip' }),
  )
  await wrapper.findComponent(FileUpload).vm.$emit('select', { originalEvent: new Event('change'), files })
  await flushPromises()
}

const button = (wrapper: Wrapper, label: string) => wrapper.findAll('button').find((b) => b.text().includes(label))!

/** Click Restore and answer the confirmation. */
async function restoreAnswering(wrapper: Wrapper, answer: boolean) {
  const confirm = useConfirmStore()
  const clicked = button(wrapper, 'Restore this backup').trigger('click')
  await flushPromises()
  confirm.settle(answer)
  await clicked
  await flushPromises()
}

async function inReview(shown: RestoreSummary = summary()) {
  vi.mocked(uploadBackupFile).mockResolvedValue('u1')
  vi.mocked(inspectBackup).mockResolvedValue(shown)
  const wrapper = mountSection()
  await choose(wrapper)
  return wrapper
}

describe('RestoreSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(uploadBackupFile).mockReset()
    vi.mocked(inspectBackup).mockReset()
    vi.mocked(restoreBackup).mockReset()
    vi.mocked(discardBackupUpload).mockReset().mockResolvedValue({ discarded: true })
  })

  it('starts by asking for a file, and says what restoring will and will not do', () => {
    const wrapper = mountSection()
    expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    expect(wrapper.findComponent(FileUpload).props('chooseLabel')).toBe('Choose backup file…')
    expect(wrapper.text()).toContain('never overwrites or deletes what you have')
  })

  it('refuses a file that is not a zip before sending it anywhere', async () => {
    const wrapper = mountSection()
    await choose(wrapper, 'photo.jpg')
    expect(uploadBackupFile).not.toHaveBeenCalled()
    expect(wrapper.find('.restore-error').text()).toContain('not a .zip file')
  })

  it('ignores a selection with nothing in it', async () => {
    const wrapper = mountSection()
    await wrapper.findComponent(FileUpload).vm.$emit('select', { originalEvent: new Event('change') })
    await flushPromises()
    expect(uploadBackupFile).not.toHaveBeenCalled()
    expect(wrapper.find('.restore-error').exists()).toBe(false)
  })

  it('accepts a zip whatever the case of its extension', async () => {
    vi.mocked(uploadBackupFile).mockResolvedValue('u1')
    vi.mocked(inspectBackup).mockResolvedValue(summary())
    const wrapper = mountSection()
    await choose(wrapper, 'BACKUP.ZIP')
    expect(uploadBackupFile).toHaveBeenCalled()
  })

  it('shows the file going up, and how far', async () => {
    const sending = deferred<string>()
    vi.mocked(uploadBackupFile).mockImplementation((_file, onProgress) => {
      onProgress(5, 10)
      return sending.promise
    })
    const wrapper = mountSection()
    await choose(wrapper, 'my-backup.zip')

    expect(wrapper.find('.restore-progress').text()).toContain('my-backup.zip')
    expect(wrapper.findComponent(ProgressBar).props('value')).toBe(50)
    expect(wrapper.find('.restore-choose').isVisible()).toBe(false)

    vi.mocked(inspectBackup).mockResolvedValue(summary())
    sending.resolve('u1')
    await flushPromises()
    expect(wrapper.find('.restore-progress').exists()).toBe(false)
  })

  it('shows the file being checked once it has arrived', async () => {
    vi.mocked(uploadBackupFile).mockResolvedValue('u1')
    const checking = deferred<RestoreSummary>()
    vi.mocked(inspectBackup).mockReturnValue(checking.promise)
    const wrapper = mountSection()
    await choose(wrapper, 'my-backup.zip')

    expect(wrapper.find('.restore-working').text()).toContain('Checking my-backup.zip')
    expect(inspectBackup).toHaveBeenCalledWith('u1')

    checking.resolve(summary())
    await flushPromises()
    expect(wrapper.find('.restore-working').exists()).toBe(false)
  })

  it('shows the summary and waits to be told to restore', async () => {
    const wrapper = await inReview()
    expect(wrapper.find('.restore-summary').exists()).toBe(true)
    expect(wrapper.find('.restore-summary').text()).toContain('What restoring will do')
    expect(restoreBackup).not.toHaveBeenCalled()
  })

  describe('confirming', () => {
    it('asks first, saying how much will be added and that nothing is overwritten', async () => {
      const wrapper = await inReview()
      const confirm = useConfirmStore()
      const clicked = button(wrapper, 'Restore this backup').trigger('click')
      await flushPromises()

      expect(confirm.open).toBe(true)
      expect(confirm.title).toBe('Restore this backup?')
      expect(confirm.message).toBe(
        'This adds 180 records to this install, 100 clips of them. ' +
          'Nothing that is already here is overwritten or deleted.',
      )
      confirm.settle(false)
      await clicked
    })

    it('says which settings the restore will overwrite when told to replace them', async () => {
      const wrapper = await inReview()
      await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', true)
      const confirm = useConfirmStore()
      const clicked = button(wrapper, 'Restore this backup').trigger('click')
      await flushPromises()
      expect(confirm.message).toContain('apart from the settings and pictures you chose to replace.')
      confirm.settle(false)
      await clicked
    })

    it('restores nothing, and stays on the summary, if the answer is no', async () => {
      const wrapper = await inReview()
      await restoreAnswering(wrapper, false)
      expect(restoreBackup).not.toHaveBeenCalled()
      expect(wrapper.find('.restore-summary').exists()).toBe(true)
    })
  })

  describe('restoring', () => {
    it('does it, reports it, and refreshes the rest of the app', async () => {
      const wrapper = await inReview()
      const working = deferred<RestoreReport>()
      vi.mocked(restoreBackup).mockReturnValue(working.promise)
      const refresh = useRefreshStore()
      const before = refresh.tick

      const confirm = useConfirmStore()
      const clicked = button(wrapper, 'Restore this backup').trigger('click')
      await flushPromises()
      confirm.settle(true)
      await clicked
      await flushPromises()

      expect(restoreBackup).toHaveBeenCalledWith('u1', false)
      expect(wrapper.find('.restore-working').text()).toContain('Restoring…')
      expect(wrapper.find('.restore-summary').exists()).toBe(false)

      working.resolve(report())
      await flushPromises()

      expect(wrapper.find('.restore-report').exists()).toBe(true)
      expect(wrapper.find('.report-verdict').text()).toContain('verified')
      expect(refresh.tick).toBe(before + 1)
    })

    it('sends the choice to replace what is already here', async () => {
      const wrapper = await inReview()
      vi.mocked(restoreBackup).mockResolvedValue(report())
      await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', true)
      await restoreAnswering(wrapper, true)
      expect(restoreBackup).toHaveBeenCalledWith('u1', true)
    })

    it('offers to restore another, from the start', async () => {
      const wrapper = await inReview()
      vi.mocked(restoreBackup).mockResolvedValue(report())
      await restoreAnswering(wrapper, true)

      await button(wrapper, 'Restore another backup').trigger('click')

      expect(wrapper.find('.restore-report').exists()).toBe(false)
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('keeps the summary, and says why, when the restore fails', async () => {
      const wrapper = await inReview()
      vi.mocked(restoreBackup).mockRejectedValue(
        new ApiError(500, '500: x', 'The restore failed. Nothing was changed.'),
      )
      await restoreAnswering(wrapper, true)
      expect(wrapper.find('.restore-error').text()).toBe('The restore failed. Nothing was changed.')
      // Still there to try again once the cause is dealt with.
      expect(wrapper.find('.restore-summary').exists()).toBe(true)
    })

    it('has a fallback when the failure carries no reason', async () => {
      const wrapper = await inReview()
      vi.mocked(restoreBackup).mockRejectedValue(new TypeError('Failed to fetch'))
      await restoreAnswering(wrapper, true)
      expect(wrapper.find('.restore-error').text()).toContain('The restore failed')
    })

    it('starts over, saying why, when the add-on no longer has the upload', async () => {
      const wrapper = await inReview()
      vi.mocked(restoreBackup).mockRejectedValue(
        new ApiError(404, '404: x', 'That upload is no longer available. Choose the file again.'),
      )
      await restoreAnswering(wrapper, true)
      expect(wrapper.find('.restore-error').text()).toContain('no longer available')
      expect(wrapper.find('.restore-summary').exists()).toBe(false)
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('does nothing if there is no summary to restore', async () => {
      const wrapper = mountSection()
      // Only reachable by calling it, never from the page.
      await (wrapper.vm as unknown as { restore: () => Promise<void> }).restore()
      expect(restoreBackup).not.toHaveBeenCalled()
    })
  })

  describe('when sending or reading the file fails', () => {
    it('says why and starts over', async () => {
      vi.mocked(uploadBackupFile).mockRejectedValue(
        new ApiError(413, '413: x', 'That file is larger than the 1 GiB a backup can be.'),
      )
      const wrapper = mountSection()
      await choose(wrapper)
      expect(wrapper.find('.restore-error').text()).toContain('larger than the 1 GiB')
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
      // The upload never got an id to throw away.
      expect(discardBackupUpload).not.toHaveBeenCalled()
    })

    it('says why a file that is not a backup was refused, and lets go of it', async () => {
      vi.mocked(uploadBackupFile).mockResolvedValue('u1')
      vi.mocked(inspectBackup).mockRejectedValue(
        new ApiError(422, '422: x', 'That file is not a readable backup. It may be damaged, or not a backup at all.'),
      )
      const wrapper = mountSection()
      await choose(wrapper)
      expect(wrapper.find('.restore-error').text()).toContain('not a readable backup')
      expect(discardBackupUpload).toHaveBeenCalledWith('u1')
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('still starts over if the add-on is too unwell to be told to let go', async () => {
      vi.mocked(uploadBackupFile).mockResolvedValue('u1')
      vi.mocked(inspectBackup).mockRejectedValue(new TypeError('Failed to fetch'))
      vi.mocked(discardBackupUpload).mockRejectedValue(new TypeError('Failed to fetch'))
      const wrapper = mountSection()
      await choose(wrapper)
      expect(wrapper.find('.restore-error').text()).toContain('Could not read that file.')
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('clears an earlier complaint when a new file is chosen', async () => {
      const wrapper = mountSection()
      await choose(wrapper, 'photo.jpg')
      expect(wrapper.find('.restore-error').exists()).toBe(true)
      vi.mocked(uploadBackupFile).mockResolvedValue('u1')
      vi.mocked(inspectBackup).mockResolvedValue(summary())
      await choose(wrapper, 'backup.zip')
      expect(wrapper.find('.restore-error').exists()).toBe(false)
    })
  })

  describe('cancelling', () => {
    it('stops a send that is under way, without calling it an error', async () => {
      let signal: AbortSignal | undefined
      vi.mocked(uploadBackupFile).mockImplementation(
        (_file, _progress, given) =>
          new Promise((_resolve, reject) => {
            signal = given
            given?.addEventListener('abort', () => reject(new DOMException('cancelled', 'AbortError')))
          }),
      )
      const wrapper = mountSection()
      await choose(wrapper)

      await button(wrapper, 'Cancel').trigger('click')
      await flushPromises()

      expect(signal?.aborted).toBe(true)
      expect(wrapper.find('.restore-error').exists()).toBe(false)
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('throws away a file that has been looked over but not restored', async () => {
      const wrapper = await inReview()
      await button(wrapper, 'Cancel').trigger('click')
      await flushPromises()
      expect(discardBackupUpload).toHaveBeenCalledWith('u1')
      expect(wrapper.find('.restore-summary').exists()).toBe(false)
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })

    it('carries on if the add-on cannot be told to throw it away', async () => {
      const wrapper = await inReview()
      vi.mocked(discardBackupUpload).mockRejectedValue(new TypeError('offline'))
      await button(wrapper, 'Cancel').trigger('click')
      await flushPromises()
      expect(wrapper.find('.restore-choose').isVisible()).toBe(true)
    })
  })

  describe('leaving the tab', () => {
    it('throws away a file that was looked over but not restored', async () => {
      const wrapper = await inReview()
      wrapper.unmount()
      expect(discardBackupUpload).toHaveBeenCalledWith('u1')
    })

    it('does not mind if the add-on cannot be reached to do so', async () => {
      const wrapper = await inReview()
      vi.mocked(discardBackupUpload).mockRejectedValue(new TypeError('offline'))
      wrapper.unmount()
      await flushPromises()
      expect(discardBackupUpload).toHaveBeenCalledTimes(1)
    })

    it('stops a send that is under way', async () => {
      let signal: AbortSignal | undefined
      vi.mocked(uploadBackupFile).mockImplementation((_file, _progress, given) => {
        signal = given
        return new Promise(() => undefined)
      })
      const wrapper = mountSection()
      await choose(wrapper)
      wrapper.unmount()
      expect(signal?.aborted).toBe(true)
    })

    it('leaves a restore that is not under way alone', () => {
      const wrapper = mountSection()
      wrapper.unmount()
      expect(discardBackupUpload).not.toHaveBeenCalled()
    })
  })

  it('shows the faces and everything else in the summary it was given', async () => {
    const wrapper = await inReview(
      summary({ tables: [row('clips', 3, 3, 'Clips'), row('face_enrollments', 2, 2, 'Enrolled faces')] }),
    )
    expect(wrapper.find('[data-table="face_enrollments"]').exists()).toBe(true)
  })
})
