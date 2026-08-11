import { defineConfig, devices } from '@playwright/test'

// Distinct from tests/conftest.py's blink_clips_test — lets this suite run
// locally alongside `pytest` against the same Postgres instance without
// the two colliding.
const DB_DSN = process.env.E2E_DATABASE_DSN ?? 'postgresql://postgres:postgres@localhost:5432/blink_clips_e2e'
const PORT = 8199

// Real interaction tests against a real (seeded) backend — see
// scripts/standalone_server.py. Distinct from ../e2e/, which smoke-tests
// that the packaged Docker image boots at all; this suite is about
// specific web UI workflows actually working end to end.
export default defineConfig({
  testDir: './e2e',
  // The backend is one shared standalone server + database for the whole
  // run (not spun up fresh per test), so tests must not run concurrently
  // against it — two tests mutating/asserting on the same seeded clip at
  // once would be a race, not a real failure.
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: `python scripts/standalone_server.py ${PORT}`,
    cwd: '..',
    url: `http://localhost:${PORT}/health`,
    // Never reuse a stray already-running instance in CI — a leftover
    // process from a previous run would still "pass" the health check
    // without ever getting the fresh TRUNCATE+seed this run's tests
    // expect.
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
    env: { BLINK_DB_DSN: DB_DSN },
    stdout: 'pipe',
    stderr: 'pipe',
  },
})
