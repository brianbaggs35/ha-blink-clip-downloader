import { test, expect } from './coverage-fixtures'

type JsonValue = Record<string, unknown> | unknown[]

async function fulfillJson(route: import('@playwright/test').Route, body: JsonValue, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

async function patchAiStatus(page: import('@playwright/test').Page, patch: Record<string, unknown>) {
  await page.route('**/api/ai/status', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback()
      return
    }
    const response = await route.fetch()
    const status = (await response.json()) as Record<string, unknown>
    await route.fulfill({ response, json: { ...status, ...patch } })
  })
}

async function mockGDriveApi(page: import('@playwright/test').Page, options: { connected?: boolean } = {}) {
  let connected = options.connected ?? true
  let connectPhase = 'idle'
  let folder = { id: 'folder-root', name: 'E2E Backups' }
  let folders = [
    { id: folder.id, name: folder.name, modified_time: '2026-01-01T00:00:00Z' },
    { id: 'folder-archive', name: 'Archive', modified_time: '' },
  ]

  await page.route('**/api/storage/gdrive/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/storage/gdrive/settings' && method === 'GET') {
      await fulfillJson(route, { client_id: 'e2e-client', has_client_secret: true, backup_policy: 'archived_only' })
      return
    }
    if (url.pathname === '/api/storage/gdrive/settings' && method === 'PUT') {
      await fulfillJson(route, { saved: true })
      return
    }
    if (url.pathname === '/api/storage/gdrive/status') {
      await fulfillJson(route, {
        configured: true,
        connected,
        account_email: connected ? 'e2e@example.com' : '',
        folder_id: connected ? folder.id : '',
        folder_name: connected ? folder.name : '',
      })
      return
    }
    if (url.pathname === '/api/storage/gdrive/connect' && method === 'POST') {
      connectPhase = 'pending'
      await fulfillJson(route, {
        phase: 'pending',
        user_code: 'E2E-CODE',
        verification_url: 'https://example.test/device',
        expires_in: 1800,
      })
      return
    }
    if (url.pathname === '/api/storage/gdrive/connect-status' && method === 'GET') {
      if (connectPhase === 'pending') {
        connectPhase = 'connected'
        connected = true
      }
      await fulfillJson(route, connectPhase === 'connected' ? { phase: 'connected' } : { phase: connectPhase })
      return
    }
    if (url.pathname === '/api/storage/gdrive/quota') {
      await fulfillJson(route, {
        available: true,
        limit: 10_000_000_000,
        usage: 2_500_000_000,
        usage_in_drive: 2_500_000_000,
      })
      return
    }
    if (url.pathname === '/api/storage/gdrive/queue/failed') {
      await fulfillJson(route, [
        {
          clip_id: 'e2e-clip-000',
          camera: 'Front Door',
          clip_path: '/share/blink-clips/e2e-clip-000.mp4',
          error_message: 'mocked quota exceeded',
          completed_at: '2026-01-01T00:00:00Z',
        },
      ])
      return
    }
    if (url.pathname === '/api/storage/gdrive/queue') {
      await fulfillJson(route, { connected, pending: 2, processing: 1, completed: 5, failed: 1 })
      return
    }
    if (url.pathname === '/api/storage/gdrive/folders' && method === 'GET') {
      await fulfillJson(route, { folders })
      return
    }
    if (url.pathname === '/api/storage/gdrive/folders' && method === 'POST') {
      const body = request.postDataJSON() as { name: string }
      const newFolder = { id: 'folder-new', name: body.name, modified_time: '' }
      folders = [...folders, newFolder]
      await fulfillJson(route, newFolder)
      return
    }
    if (url.pathname === '/api/storage/gdrive/folder' && method === 'PUT') {
      const body = request.postDataJSON() as { folder_id: string; folder_name: string }
      folder = { id: body.folder_id, name: body.folder_name }
      await fulfillJson(route, { saved: true })
      return
    }
    if (url.pathname === '/api/storage/gdrive/retry' && method === 'POST') {
      await fulfillJson(route, { retried: 1 })
      return
    }
    if (url.pathname === '/api/storage/gdrive/backup-now' && method === 'POST') {
      await fulfillJson(route, { enqueued: 3 })
      return
    }
    if (url.pathname === '/api/storage/gdrive/upload' && method === 'POST') {
      await fulfillJson(route, { enqueued: 1 })
      return
    }
    if (url.pathname === '/api/storage/gdrive/disconnect' && method === 'POST') {
      connected = false
      await fulfillJson(route, { disconnected: true })
      return
    }
    await route.fallback()
  })
}

test('covers AI configuration, feedback, email alerts, and model-picker success states with stubs', async ({
  page,
}) => {
  await patchAiStatus(page, { smtp_configured: true })
  await page.route('**/api/notifications/test-email', (route) =>
    fulfillJson(route, { success: false, message: 'Mock SMTP rejected the test message' }),
  )
  await page.route('**/api/ai/feedback/stats', (route) =>
    fulfillJson(route, { total: 4, correct: 3, incorrect: 1, false_positive: 1, false_negative: 0 }),
  )
  await page.route('**/api/ai/suspicious*', (route) =>
    fulfillJson(route, {
      items: [
        {
          clip_id: 'e2e-clip-000',
          camera: 'Front Door',
          confidence: 0.82,
          summary: 'Mocked suspicious activity',
          analyzed_at: '2026-01-01T00:00:00Z',
        },
      ],
      total: 1,
    }),
  )
  await page.route('**/api/ai/feedback/e2e-clip-000', (route) => fulfillJson(route, { saved: true }))
  await page.route('**/api/ai/models', (route) =>
    fulfillJson(route, {
      enabled: true,
      models: [
        { name: 'llava:latest', size: 2_000_000_000 },
        { name: 'moondream', size: 1_000_000_000 },
      ],
    }),
  )
  await page.route('**/api/ai/models/escalation', (route) =>
    fulfillJson(route, { models: [{ name: 'llava:escalation', size: 3_000_000_000 }] }),
  )

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  await expect(page.getByText('1 false positive(s), 0 false negative(s) reported')).toBeVisible()
  const suspicious = page.locator('.card', { hasText: 'Mocked suspicious activity' })
  await expect(suspicious).toContainText('82%')
  await suspicious.locator('button[title="Correct"]').click()
  await expect(suspicious).toContainText('Thanks!')

  await page.locator('#suspicious-period-filter').click()
  await page.getByRole('option', { name: 'Today' }).click()
  await expect(suspicious).toBeVisible()

  await page.getByRole('button', { name: 'Configure Cameras' }).click()
  const configDialog = page.getByRole('dialog', { name: 'AI Analysis Configuration' })
  await expect(configDialog).toBeVisible()
  const firstCameraToggle = configDialog.locator('input[role="switch"]').nth(1)
  const initialEnabled = await firstCameraToggle.isChecked()
  await firstCameraToggle.click()
  await configDialog.getByRole('button', { name: 'Save Settings' }).click()
  await expect(page.getByText('AI analysis settings saved')).toBeVisible()

  await page.getByRole('button', { name: '✉️ Send Test Email' }).click()
  await expect(page.getByText('Mock SMTP rejected the test message')).toBeVisible()

  await page.getByRole('button', { name: '⟳ Fetch Models' }).click()
  await expect(page.getByText('Found 2 vision model(s)')).toBeVisible()
  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.getByRole('button', { name: '📋 Copy' }).click()
  await expect(page.getByText('Copied "llava:latest"')).toBeVisible()

  await page.locator('#cam-prompt-Garage').fill('  Mocked garage prompt  ')
  await page.getByRole('button', { name: '💾 Save Camera Configs' }).click()
  await expect(page.getByText('Camera configs saved')).toBeVisible()

  await suspicious.click()
  const suspiciousModal = page.locator('.modal-bg.open')
  await expect(suspiciousModal).toBeVisible()
  await suspiciousModal.locator('.modal-close').click()

  await patchAiStatus(page, {
    escalation_provider: 'ollama',
    escalation_model: 'llava:escalation',
    escalation_online: false,
  })
  await page.reload()
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.getByRole('button', { name: '⟳ Fetch Escalation Models' }).click()
  await expect(page.getByText('Found 1 escalation model(s)')).toBeVisible()
  await page.getByRole('button', { name: '📋 Copy' }).last().click()
  await expect(page.getByText('Copied "llava:escalation"')).toBeVisible()

  await page.getByRole('button', { name: 'Configure Cameras' }).click()
  const restoreDialog = page.getByRole('dialog', { name: 'AI Analysis Configuration' })
  const restoreToggle = restoreDialog.locator('input[role="switch"]').nth(1)
  if ((await restoreToggle.isChecked()) !== initialEnabled) {
    await restoreToggle.click()
    await restoreDialog.getByRole('button', { name: 'Save Settings' }).click()
    await expect(page.getByText('AI analysis settings saved')).toBeVisible()
  } else {
    await restoreDialog.getByRole('button', { name: 'Cancel' }).click()
  }
})

test('shows a successful mocked email alert result', async ({ page }) => {
  await patchAiStatus(page, { smtp_configured: true })
  await page.route('**/api/notifications/test-email', (route) =>
    fulfillJson(route, { success: true, message: 'Mock SMTP accepted the test message' }),
  )

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await page.getByRole('button', { name: '✉️ Send Test Email' }).click()
  await expect(page.getByText('✓ Mock SMTP accepted the test message')).toBeVisible()
})

test('shows the unsupported-architecture state for mocked local Moondream', async ({ page }) => {
  await patchAiStatus(page, { provider: 'moondream_local', moondream_arch_supported: false })
  await page.route('**/api/ai/moondream/install-status', (route) =>
    fulfillJson(route, {
      installed: false,
      arch_supported: false,
      install_state: { status: 'unsupported' },
    }),
  )
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await expect(page.getByText('moondream_local is not available on this architecture.')).toBeVisible()
})

test('covers AI analysis configuration load-error, retry, and empty states', async ({ page }) => {
  let cameraConfigReads = 0
  await page.route('**/api/ai/camera-configs', async (route) => {
    cameraConfigReads++
    if (cameraConfigReads === 1) {
      await fulfillJson(route, { error: 'mocked unavailable' }, 500)
      return
    }
    await fulfillJson(route, [])
  })

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')
  await expect(page.getByText('Unable to load camera settings.')).toBeVisible()

  await page.getByRole('button', { name: 'Configure Cameras' }).click()
  const dialog = page.getByRole('dialog', { name: 'AI Analysis Configuration' })
  await expect(dialog).toContainText('Camera settings could not be loaded.')
  await dialog.getByRole('button', { name: 'Retry' }).click()
  await expect(dialog).toContainText('No cameras found. Download at least one clip first.')
})

test('exercises mocked Moondream fine-tuning controls without cloud credentials', async ({ page }) => {
  await patchAiStatus(page, { provider: 'moondream_cloud', smtp_configured: false })

  let finetunes: Array<{ finetune_id: string; name: string }> = []
  await page.route('**/api/ai/finetune**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const method = request.method()

    if (url.pathname === '/api/ai/finetune' && method === 'GET') {
      await fulfillJson(route, { enabled: true, finetunes })
      return
    }
    if (url.pathname === '/api/ai/finetune' && method === 'POST') {
      finetunes = [{ finetune_id: 'e2e-finetune', name: 'E2E Fine-tune' }]
      await fulfillJson(route, { finetune_id: 'e2e-finetune' })
      return
    }
    if (url.pathname === '/api/ai/finetune/e2e-finetune/train') {
      await fulfillJson(route, { trained: 2 })
      return
    }
    if (url.pathname === '/api/ai/finetune/e2e-finetune/save-checkpoint') {
      await fulfillJson(route, { saved: true })
      return
    }
    if (url.pathname === '/api/ai/finetune/e2e-finetune/checkpoints') {
      await fulfillJson(route, { enabled: true, checkpoints: [{ step: 4 }, { step: 8 }] })
      return
    }
    if (url.pathname === '/api/ai/finetune/e2e-finetune/activate') {
      await fulfillJson(route, { activated: true, model: 'e2e-finetune-step-8' })
      return
    }
    if (url.pathname === '/api/ai/finetune/e2e-finetune' && method === 'DELETE') {
      finetunes = []
      await fulfillJson(route, { deleted: true })
      return
    }
    await route.fallback()
  })
  await page.route('**/api/ai/feedback/untrained-count', (route) => fulfillJson(route, { count: 2 }))

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="ai"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="ai"]')

  const fineTuneCard = page.locator('.card').filter({ has: page.locator('h3', { hasText: 'Fine-Tuning' }) })
  await expect(fineTuneCard).toContainText('No fine-tunes yet')
  await fineTuneCard.locator('#finetune-new-name').fill('E2E Fine-tune')
  await fineTuneCard.getByRole('button', { name: '+ New Fine-tune' }).click()
  await expect(fineTuneCard).toContainText('E2E Fine-tune')

  await fineTuneCard.getByRole('button', { name: /Train from Feedback/ }).click()
  await expect(page.getByText('Trained 2 step(s)')).toBeVisible()
  await fineTuneCard.getByRole('button', { name: '💾 Save Checkpoint' }).click()
  await expect(page.getByText('Checkpoint saved')).toBeVisible()

  await fineTuneCard.getByRole('button', { name: 'Checkpoints' }).click()
  await expect(fineTuneCard).toContainText('Step 4')
  await fineTuneCard.getByRole('button', { name: 'Activate' }).last().click()
  await expect(page.getByText('Activated: e2e-finetune-step-8')).toBeVisible()
  await fineTuneCard.getByRole('button', { name: '← Back to fine-tunes' }).click()

  await fineTuneCard.locator('button').filter({ hasText: '🗑' }).click()
  await expect(page.getByText('Delete this fine-tune and all its checkpoints? This cannot be undone.')).toBeVisible()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(page.getByText('Fine-tune deleted')).toBeVisible()
  await expect(fineTuneCard).toContainText('No fine-tunes yet')
})

test('covers connected Google Drive, folder management, retries, and library upload with stubs', async ({ page }) => {
  await mockGDriveApi(page)
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')

  const connection = page.locator('.gdrive-connection-card')
  await expect(connection).toContainText('Connected as e2e@example.com')
  await expect(connection).toContainText('25%')
  await expect(connection).toContainText('mocked quota exceeded')

  await connection.getByRole('button', { name: 'Retry', exact: true }).click()
  await expect(page.getByText('Retrying upload')).toBeVisible()
  await connection.getByRole('button', { name: 'Retry All Failed' }).click()
  await expect(page.getByText('Retrying 1 upload(s)')).toBeVisible()
  await connection.getByRole('button', { name: 'Back Up Existing Clips Now' }).click()
  await expect(page.getByText('Queued 3 clip(s) for backup')).toBeVisible()

  await connection.getByRole('button', { name: 'Change Folder' }).click()
  const folderDialog = page.getByRole('dialog', { name: 'Change Backup Folder' })
  await folderDialog.getByRole('button', { name: 'New Folder' }).click()
  const newFolderDialog = page.getByRole('dialog', { name: 'New Folder' })
  await newFolderDialog.locator('input').fill('New E2E Folder')
  await newFolderDialog.getByRole('button', { name: 'Create' }).click()
  await expect(page.getByText('Created folder "New E2E Folder"')).toBeVisible()
  await folderDialog.getByRole('button', { name: 'New E2E Folder' }).click()
  await folderDialog.getByRole('button', { name: 'Use This Folder' }).click()
  await expect(page.getByText('Backup folder: New E2E Folder')).toBeVisible()

  await page.goto('/')
  await page.waitForSelector('.app-nav-tab.active[data-tab="library"]')
  const firstClip = page.locator('.clip-card').first()
  await firstClip.locator('.sel-check').click()
  await page.getByRole('button', { name: '☁ Upload to Drive' }).click()
  const uploadDialog = page.getByRole('dialog', { name: /Upload 1 clip\(s\) to Google Drive/ })
  await expect(uploadDialog).toBeVisible()
  await uploadDialog.getByRole('button', { name: 'Select' }).first().click()
  await expect(page.getByText(/Queued 1 clip\(s\) for upload to/)).toBeVisible()

  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')
  await page.locator('.gdrive-connection-card').getByRole('button', { name: 'Disconnect' }).click()
  await page.getByRole('button', { name: 'Confirm' }).click()
  await expect(
    page.locator('.gdrive-connection-card').getByRole('button', { name: 'Connect Google Drive' }),
  ).toBeVisible()
})

test('completes a mocked Google Drive device-code connection', async ({ page }) => {
  await mockGDriveApi(page, { connected: false })
  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="storage"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="storage"]')

  const connection = page.locator('.gdrive-connection-card')
  await connection.getByRole('button', { name: 'Connect Google Drive' }).click()
  await expect(connection).toContainText('E2E-CODE')
  await expect(connection).toContainText('Waiting for confirmation')
  await page.waitForTimeout(3500)
  await expect(connection).toContainText('Connected as e2e@example.com')
})

test('shows the vehicle settings load failure from mocked API errors', async ({ page }) => {
  await page.route('**/api/vehicle/settings', (route) => fulfillJson(route, { error: 'mocked unavailable' }, 500))
  await page.route('**/api/ai/camera-configs', (route) => fulfillJson(route, { error: 'mocked unavailable' }, 500))

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')
  await expect(page.getByText('Failed to load vehicle settings')).toBeVisible()
})

test('shows the vehicle empty state when mocked APIs return no cameras', async ({ page }) => {
  await page.route('**/api/vehicle/settings', (route) => fulfillJson(route, { car_description: '' }))
  await page.route('**/api/ai/camera-configs', (route) => fulfillJson(route, []))

  await page.goto('/')
  await page.locator('.app-nav-tab[data-tab="vehicles"]').click()
  await page.waitForSelector('.app-nav-tab.active[data-tab="vehicles"]')
  await expect(page.getByText('No cameras found. Download at least one clip first.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Save Camera Settings' })).toHaveCount(0)
})
