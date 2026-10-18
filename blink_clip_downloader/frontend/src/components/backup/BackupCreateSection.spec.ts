import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import ToggleSwitch from 'primevue/toggleswitch'
import BackupCreateSection from './BackupCreateSection.vue'
import { preview } from './testing'
import { ApiError } from '../../api/client'
import type { BackupPreview } from '../../api/types'
import { backupToGDrive, createBackup, getBackupPreview } from '../../api/backup'
import { useRefreshStore } from '../../stores/refresh'
import { useToastStore } from '../../stores/toast'
import { saveBlob } from './saveBlob'

vi.mock('../../api/backup', () => ({
  getBackupPreview: vi.fn(),
  createBackup: vi.fn(),
  backupToGDrive: vi.fn(),
}))
vi.mock('./saveBlob', () => ({ saveBlob: vi.fn() }))

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

async function mountSection(shown: BackupPreview | null = preview()) {
  vi.mocked(getBackupPreview).mockImplementation(() =>
    shown ? Promise.resolve(shown) : Promise.reject(new Error('offline')),
  )
  const wrapper = mount(BackupCreateSection)
  await flushPromises()
  return wrapper
}

const buttons = (wrapper: Awaited<ReturnType<typeof mountSection>>) => wrapper.findAll('.backup-actions button')

describe('BackupCreateSection', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.mocked(getBackupPreview).mockReset()
    vi.mocked(createBackup).mockReset()
    vi.mocked(backupToGDrive).mockReset()
    vi.mocked(saveBlob).mockReset()
  })

  it('shows a spinner until it knows what a backup would hold', async () => {
    const pending = deferred<BackupPreview>()
    vi.mocked(getBackupPreview).mockReturnValue(pending.promise)
    const wrapper = mount(BackupCreateSection)
    expect(wrapper.find('.loading-indicator').exists()).toBe(true)
    pending.resolve(preview())
    await flushPromises()
    expect(wrapper.find('.loading-indicator').exists()).toBe(false)
  })

  it('lists what the backup will hold, leaving out what is empty', async () => {
    const wrapper = await mountSection()
    const listed = wrapper.findAll('.include-list li').map((li) => [li.find('span').text(), li.find('strong').text()])
    expect(listed).toEqual([
      ['Clips', '1,204'],
      ['AI analyses', '980'],
      ['Enrolled faces', '3'],
      ['Settings and reference pictures', '3'],
    ])
  })

  it('says what is not in a backup and where the videos stay', async () => {
    const text = (await mountSection()).find('.not-included').text()
    expect(text).toContain('/share/blink-clips')
    expect(text).toContain('every password and sign-in')
  })

  it('says so when there is nothing to back up yet', async () => {
    const wrapper = await mountSection(
      preview({ tables: [{ name: 'clips', label: 'Clips', rows: 0, biometric: false }], files: [] }),
    )
    expect(wrapper.text()).toContain('Nothing to back up yet.')
  })

  it('says so when it cannot tell what a backup would hold', async () => {
    const wrapper = await mountSection(null)
    expect(wrapper.text()).toContain('Could not read what a backup would hold')
    expect(wrapper.find('.backup-actions').exists()).toBe(false)
  })

  it('leaves the faces out of the list, and the backup, when asked', async () => {
    const wrapper = await mountSection()
    await wrapper.findComponent(ToggleSwitch).vm.$emit('update:modelValue', false)
    expect(wrapper.find('[data-table="face_enrollments"]').exists()).toBe(false)

    vi.mocked(createBackup).mockResolvedValue({ blob: new Blob(['zip']), filename: 'b.zip' })
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(createBackup).toHaveBeenCalledWith(false)
  })

  it('explains what including the faces means', async () => {
    const text = (await mountSection()).find('.faces-option').text()
    expect(text).toContain('Include enrolled faces')
    expect(text).toContain('never leave this device on their own')
    expect(text).toContain('not encrypted')
  })

  it('offers no choice about faces when none are enrolled', async () => {
    const wrapper = await mountSection(
      preview({ tables: [{ name: 'face_enrollments', label: 'Enrolled faces', rows: 0, biometric: true }] }),
    )
    expect(wrapper.find('.faces-option').exists()).toBe(false)
  })

  it('downloads a backup under the name the add-on gave it', async () => {
    const blob = new Blob([new Uint8Array(2048)])
    vi.mocked(createBackup).mockResolvedValue({ blob, filename: 'blink-clips-backup-1.zip' })
    const wrapper = await mountSection()

    await buttons(wrapper)[0].trigger('click')
    await flushPromises()

    expect(createBackup).toHaveBeenCalledWith(true)
    expect(saveBlob).toHaveBeenCalledWith(blob, 'blink-clips-backup-1.zip')
    expect(wrapper.find('.backup-done').text()).toBe(
      'Downloaded blink-clips-backup-1.zip (2 KB). Keep it somewhere safe.',
    )
    expect(useToastStore().message).toBe('Backup complete')
  })

  it('says why a backup could not be made', async () => {
    vi.mocked(createBackup).mockRejectedValue(
      new ApiError(500, '500: x', 'Creating the backup failed: No space left on device.'),
    )
    const wrapper = await mountSection()
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.backup-error').text()).toBe('Creating the backup failed: No space left on device.')
    expect(wrapper.find('.backup-done').exists()).toBe(false)
  })

  it('has a fallback when the failure carries no reason', async () => {
    vi.mocked(createBackup).mockRejectedValue(new TypeError('Failed to fetch'))
    const wrapper = await mountSection()
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.backup-error').text()).toContain('Could not create the backup')
  })

  it('shows it is working, and will not start a second backup meanwhile', async () => {
    const pending = deferred<{ blob: Blob; filename: string }>()
    vi.mocked(createBackup).mockReturnValue(pending.promise)
    const wrapper = await mountSection(preview({ gdrive: { state: 'ready', folder: 'Blink Clips/Backups' } }))

    await buttons(wrapper)[0].trigger('click')

    expect(wrapper.find('.backup-working').exists()).toBe(true)
    expect(buttons(wrapper).map((b) => b.attributes('disabled') !== undefined)).toEqual([true, true])

    pending.resolve({ blob: new Blob(['x']), filename: 'b.zip' })
    await flushPromises()
    expect(wrapper.find('.backup-working').exists()).toBe(false)
    expect(buttons(wrapper).map((b) => b.attributes('disabled') !== undefined)).toEqual([false, false])
  })

  it('clears the last result when another backup starts', async () => {
    vi.mocked(createBackup).mockResolvedValueOnce({ blob: new Blob(['x']), filename: 'b.zip' })
    vi.mocked(createBackup).mockRejectedValueOnce(new TypeError('offline'))
    const wrapper = await mountSection()
    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.backup-done').exists()).toBe(true)

    await buttons(wrapper)[0].trigger('click')
    await flushPromises()
    expect(wrapper.find('.backup-done').exists()).toBe(false)
    expect(wrapper.find('.backup-error').exists()).toBe(true)
  })

  describe('Google Drive', () => {
    it('offers nothing when this install has no Drive support', async () => {
      const wrapper = await mountSection(preview({ gdrive: { state: 'unavailable', folder: '' } }))
      expect(buttons(wrapper)).toHaveLength(1)
      expect(wrapper.find('.drive-hint').exists()).toBe(false)
    })

    it.each([
      ['not_connected', 'Connect Google Drive on the Storage tab'],
      ['no_folder', 'Choose a Google Drive folder on the Storage tab'],
    ] as const)('is unavailable, and says what to do, when Drive is %s', async (state, hint) => {
      const wrapper = await mountSection(preview({ gdrive: { state, folder: '' } }))
      const drive = buttons(wrapper)[1]
      expect(drive.text()).toContain('Back up to Google Drive')
      expect(drive.attributes('disabled')).toBeDefined()
      expect(wrapper.find('.drive-hint').text()).toContain(hint)
    })

    it('saves into the Backups folder when Drive is ready', async () => {
      vi.mocked(backupToGDrive).mockResolvedValue({
        name: 'blink-clips-backup-1.zip',
        size_bytes: 3 * 1024 * 1024,
        folder: 'Blink Clips/Backups',
        file_id: 'f1',
      })
      const wrapper = await mountSection(preview({ gdrive: { state: 'ready', folder: 'Blink Clips/Backups' } }))
      expect(wrapper.find('.drive-hint').text()).toBe('Saves into Blink Clips/Backups on your Google Drive.')

      await buttons(wrapper)[1].trigger('click')
      await flushPromises()

      expect(backupToGDrive).toHaveBeenCalledWith(true)
      expect(saveBlob).not.toHaveBeenCalled()
      expect(wrapper.find('.backup-done').text()).toBe(
        'Saved to Google Drive: Blink Clips/Backups/blink-clips-backup-1.zip (3.0 MB).',
      )
    })

    it('says why Drive did not take it', async () => {
      vi.mocked(backupToGDrive).mockRejectedValue(
        new ApiError(502, '502: x', 'Google Drive is out of space, so it would not take the backup.'),
      )
      const wrapper = await mountSection(preview({ gdrive: { state: 'ready', folder: 'F/Backups' } }))
      await buttons(wrapper)[1].trigger('click')
      await flushPromises()
      expect(wrapper.find('.backup-error').text()).toContain('out of space')
    })
  })

  describe('keeping up with the library', () => {
    it('reads what a backup holds again when something refreshes the page', async () => {
      const wrapper = await mountSection()
      vi.mocked(getBackupPreview).mockResolvedValue(
        preview({ tables: [{ name: 'clips', label: 'Clips', rows: 5000, biometric: false }] }),
      )
      useRefreshStore().bump()
      await flushPromises()
      expect(wrapper.find('[data-table="clips"]').text()).toContain('5,000')
    })

    it('ignores an answer that arrives after a newer one', async () => {
      const slow = deferred<BackupPreview>()
      vi.mocked(getBackupPreview).mockReturnValueOnce(slow.promise)
      const wrapper = mount(BackupCreateSection)
      vi.mocked(getBackupPreview).mockResolvedValue(
        preview({ tables: [{ name: 'clips', label: 'Clips', rows: 7, biometric: false }] }),
      )
      useRefreshStore().bump()
      await flushPromises()

      slow.resolve(preview({ tables: [{ name: 'clips', label: 'Clips', rows: 1, biometric: false }] }))
      await flushPromises()

      expect(wrapper.find('[data-table="clips"]').text()).toContain('7')
    })

    it('keeps what it had when a refresh fails', async () => {
      const wrapper = await mountSection()
      vi.mocked(getBackupPreview).mockRejectedValue(new Error('offline'))
      useRefreshStore().bump()
      await flushPromises()
      expect(wrapper.find('[data-table="clips"]').exists()).toBe(true)
    })
  })
})
