#!/usr/bin/env node

import { appendFileSync } from 'node:fs'

import { chromium } from '@playwright/test'

import { completeOnboarding } from '../../../e2e/lib/ha_onboarding.mjs'

const [baseUrl, addonSlug, panelTitle] = process.argv.slice(2)

const envFile = process.env.GITHUB_ENV

const storageState = process.env.RUNNER_TEMP ? `${process.env.RUNNER_TEMP}/ha-playwright-storage-state.json` : undefined

const owner = {
  name: 'CI Integration Test',
  username: 'ci-integration-test',
  password: 'ci-integration-test-password-1',
}

if (!baseUrl || !addonSlug || !panelTitle || !envFile || !storageState) {
  throw new Error(
    'Usage: ha-supervisor-setup.mjs <ha-base-url> <addon-slug> <panel-title> (requires GITHUB_ENV and RUNNER_TEMP)',
  )
}

await completeOnboarding(baseUrl, owner)

const browser = await chromium.launch()
const context = await browser.newContext()
const page = await context.newPage()

try {
  await page.goto(baseUrl, {
    waitUntil: 'load',
    timeout: 20000,
  })

  await page.locator('input[name="username"]').fill(owner.username)
  await page.locator('input[name="password"]').fill(owner.password)

  await page.getByRole('button', { name: /log in/i }).click()

  await page.waitForURL(/\/home/, {
    timeout: 20000,
  })

  const panelLink = page.locator(`a[href="/${addonSlug}"]`)

  await panelLink.waitFor({
    state: 'attached',
    timeout: 15000,
  })

  await page.getByText(panelTitle, { exact: true }).click()

  await page.waitForURL(new RegExp(addonSlug), {
    timeout: 15000,
  })

  const iframe = page.locator('iframe').first()

  await iframe.waitFor({
    state: 'attached',
    timeout: 15000,
  })

  const src = await iframe.getAttribute('src')

  if (!src || !/^\/api\/hassio_ingress\/[^/]+\/?$/.test(new URL(src, baseUrl).pathname)) {
    throw new Error(`Expected a real HA ingress iframe, got ${JSON.stringify(src)}`)
  }

  const iframeUrl = new URL(src, baseUrl)
  const ingressUrl = new URL(baseUrl)
  ingressUrl.pathname = `${iframeUrl.pathname.replace(/\/$/, '')}/`
  ingressUrl.search = iframeUrl.search
  ingressUrl.hash = ''

  await context.storageState({
    path: storageState,
  })

  const directUrl = new URL(baseUrl)

  directUrl.port = process.env.ADDON_PORT ?? '8099'

  appendFileSync(
    envFile,
    `HA_E2E_INGRESS_URL=${ingressUrl.toString()}\n` +
      `HA_E2E_STORAGE_STATE=${storageState}\n` +
      `HA_E2E_DIRECT_URL=${directUrl.origin}\n` +
      `HA_E2E_USERNAME=${owner.username}\n` +
      `HA_E2E_PASSWORD=${owner.password}\n`,
  )

  console.log(`Playwright will run through Supervisor ingress at ${ingressUrl}`)
  console.log(`Playwright storage state: ${storageState}`)
  console.log(`Direct add-on URL: ${directUrl.origin}`)
} catch (error) {
  await page
    .screenshot({
      path: '../../e2e/ha-integration-failure-playwright-setup.png',
      fullPage: true,
    })
    .catch(() => {})

  throw error
} finally {
  await browser.close()
}
