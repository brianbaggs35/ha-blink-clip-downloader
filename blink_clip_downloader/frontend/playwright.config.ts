import { defineConfig, devices } from '@playwright/test'
import { SIGNED_IN_STATE } from './e2e/signin-state'

// Distinct from tests/conftest.py's blink_clips_test — lets this suite run
// locally alongside `pytest` against the same Postgres instance without
// the two colliding.
const DB_DSN = process.env.E2E_DATABASE_DSN ?? 'postgresql://postgres:postgres@localhost:5432/blink_clips_e2e'
const PORT = 8199
const HA_SUPERVISOR = process.env.BLINK_E2E_HA === '1'
const HA_INGRESS_URL = process.env.HA_E2E_INGRESS_URL
const HA_STORAGE_STATE = process.env.HA_E2E_STORAGE_STATE
// BLINK_E2E_SIGNIN=1 runs the whole suite behind Direct Access Sign-In: the
// backend asks for a sign-in on its main port (scripts/standalone_server.py)
// and e2e/global-signin.ts signs in once, so each spec meets the app as a
// browser on the add-on's direct port would. Off by default.
const SIGNED_IN = process.env.BLINK_E2E_SIGNIN === '1'

if (HA_SUPERVISOR && (!HA_INGRESS_URL || !HA_STORAGE_STATE)) {
  throw new Error('BLINK_E2E_HA=1 requires HA_E2E_INGRESS_URL and HA_E2E_STORAGE_STATE')
}

// Real interaction tests against a real (seeded) backend — normally
// scripts/standalone_server.py, or the installed add-on through real HA
// ingress when BLINK_E2E_HA=1. Distinct from ../e2e/, which smoke-tests
// that the packaged Docker image boots at all; this suite is about
// specific web UI workflows actually working end to end.
export default defineConfig({
  testDir: './e2e',
  // Keep HA-only checks out of the standalone run. The HA workflow explicitly
  // excludes @standalone tests with --grep-invert.
  grepInvert: HA_SUPERVISOR ? undefined : /@ha/,
  // The standalone sign-in run has a dedicated global setup; HA mode runs
  // that same spec against the add-on's real direct port and Supervisor auth.
  testIgnore: SIGNED_IN ? /direct-access-signin\.spec\.ts$/ : undefined,
  globalSetup: SIGNED_IN ? './e2e/global-signin.ts' : undefined,
  // The backend is one shared standalone server + database for the whole
  // run (not spun up fresh per test), so tests must not run concurrently
  // against it — two tests mutating/asserting on the same seeded clip at
  // once would be a race, not a real failure.
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  // Keep GitHub annotations for failures, but use the list reporter as the
  // primary CI output so every test is visible instead of being collapsed to
  // a row of dots.
  reporter: process.env.CI ? [['list'], ['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: HA_SUPERVISOR ? HA_INGRESS_URL : `http://localhost:${PORT}`,
    storageState: HA_SUPERVISOR ? HA_STORAGE_STATE : SIGNED_IN ? SIGNED_IN_STATE : undefined,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: HA_SUPERVISOR
    ? undefined
    : {
        command: `python scripts/standalone_server.py ${PORT}`,
        cwd: '..',
        url: `http://localhost:${PORT}/health`,
        // Never reuse a stray already-running instance in CI — a leftover
        // process from a previous run would still "pass" the health check
        // without ever getting the fresh TRUNCATE+seed this run's tests
        // expect.
        reuseExistingServer: !process.env.CI,
        timeout: 30_000,
        env: {
          BLINK_DB_DSN: DB_DSN,
          BLINK_E2E: '1',
          ...(SIGNED_IN ? { BLINK_E2E_SIGNIN: '1' } : {}),
        },
        stdout: 'pipe',
        stderr: 'pipe',
      },
})
