import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config.ts'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test-setup.ts'],
      // Reuse one VM-backed jsdom environment per worker while preserving
      // isolation between test files. This avoids creating 72 jsdom
      // environments during CI coverage runs.
      pool: 'vmThreads',
      // Half the cores, not all of them. Vitest defaults to roughly one
      // worker per core, and each one here carries a full jsdom plus
      // PrimeVue — on a many-core machine that over-subscribes badly enough
      // that timing-sensitive component tests start losing races against
      // their own overlay/transition callbacks. The result was a suite that
      // failed two-or-so tests per run, in a different file each time, and
      // passed every one of them when re-run or run alone.
      //
      // Measured on a 12-core box: unbounded ~21s with 2 failures in 4 runs
      // (idle machine, nothing else competing); '50%' ~14s with 0 in 10.
      // Fewer workers is both steadier *and* quicker here, because the
      // per-worker jsdom startup dominates at this suite's size.
      maxWorkers: '50%',
      // The residue the worker cap does not remove, which only shows up
      // under the extra load of a coverage run. Every failure seen has the
      // same shape — an assertion running before Vue has finished
      // re-rendering ("expected [ 'page' ] to include 'active'", a button
      // that findAll() cannot see yet) — and every one of them passes
      // deterministically when its file is run alone.
      //
      // This is for harness timing, not for product flakiness: a real
      // regression fails all three attempts, so nothing genuine is hidden.
      // If a test needs this retry *consistently*, that is a bug in the
      // test — make it wait for what it is asserting on instead.
      retry: 2,
      // e2e/ holds @playwright/test specs (frontend/playwright.config.ts),
      // a different test runner entirely — Vitest's default file glob
      // would otherwise pick them up and try (and fail) to run them too.
      exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**'],
      coverage: {
        provider: 'v8',
        // lcov is for SonarCloud's JS/TS analyzer (sonar.javascript.lcov.reportPaths
        // in sonar-project.properties) - cobertura is what Codecov consumes.
        reporter: ['text', 'text-summary', 'cobertura', 'lcov', 'html'],
        include: ['src/**/*.{ts,vue}'],
        // TypeScript-only declarations are erased before Vitest runs, so
        // api/types.ts has no executable statements that a test can cover.
        exclude: ['src/main.ts', 'src/test-setup.ts', 'src/vite-env.d.ts', 'src/api/types.ts', 'src/**/*.spec.ts'],
        // Mirrors the backend's pyproject.toml coverage gate (fail_under = 80)
        // so the frontend is held to the same bar as the Python package.
        thresholds: {
          lines: 80,
          statements: 80,
          functions: 80,
          branches: 80,
        },
      },
    },
  }),
)
